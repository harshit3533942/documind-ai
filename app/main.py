"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload
"""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.middleware import RequestTimingMiddleware, SecurityHeadersMiddleware
from app.database.connection import engine
from app.routers import admin, auth, chat, conversations, documents, health

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


def _preload_embedding_model() -> None:
    """Load the model in the background so the FIRST question is not slow.
    A request arriving before loading finishes simply waits on the model's
    load lock (see SentenceTransformerEmbedder._load)."""
    try:
        from app.services.embedding_service import get_embedder

        get_embedder().embed_query("warm-up")
        logger.info("Embedding model pre-loaded")
    except Exception:  # noqa: BLE001 - the first real request will report the error
        logger.exception("Embedding model pre-load failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Startup: make sure the upload directory exists.
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "%s v%s starting (%s, answer mode: %s)",
        settings.app_name, settings.app_version, settings.environment, settings.resolved_llm_provider,
    )
    if settings.preload_embedding_model:
        threading.Thread(target=_preload_embedding_model, name="model-preload", daemon=True).start()
    yield
    # Shutdown: return pooled DB connections.
    engine.dispose()
    logger.info("%s shutting down", settings.app_name)


def create_app() -> FastAPI:
    """Application factory - makes testing with different settings easy."""
    docs = settings.docs_enabled  # Swagger UI off in production (less attack surface / API map)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="AI-powered Document Intelligence Platform (RAG over your PDFs).",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestTimingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Fast reject of obviously-too-large bodies using the Content-Length header,
    # before the multipart body is received. The streaming check in
    # LocalStorage still enforces the exact limit (headers can lie). In
    # production a reverse proxy (nginx client_max_body_size) is the first line.
    body_limit = settings.max_upload_size_bytes + 1024 * 1024  # + multipart overhead

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > body_limit:
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "payload_too_large",
                                   "message": f"File is larger than {settings.max_upload_size_mb} MB"}},
            )
        return await call_next(request)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router, prefix=settings.api_v1_prefix)
    app.include_router(documents.router, prefix=settings.api_v1_prefix)
    app.include_router(chat.router, prefix=settings.api_v1_prefix)
    app.include_router(conversations.router, prefix=settings.api_v1_prefix)
    app.include_router(admin.router, prefix=settings.api_v1_prefix)

    @app.get("/", tags=["root"])
    async def root() -> dict[str, str]:
        return {"message": f"Welcome to {settings.app_name}", "docs": "/docs" if docs else "disabled"}

    return app


app = create_app()
