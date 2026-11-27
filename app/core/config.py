"""Centralised application settings.

All configuration comes from environment variables (or a local `.env` file).
Nothing secret is hardcoded here - only safe development defaults.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "DocuMind AI"
    app_version: str = "0.1.0"
    environment: Literal["development", "testing", "production"] = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # --- CORS (frontend origins allowed to call the API) ---
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Storage ---
    storage_dir: Path = BASE_DIR / "storage" / "uploads"
    max_upload_size_mb: int = Field(default=20, ge=1, le=200)
    max_pdf_pages: int = Field(default=1000, ge=1)

    # --- PDF text extraction ---
    # A document whose extracted text is shorter than this is treated as
    # "no extractable text" (typically a scanned/image-only PDF).
    min_extractable_chars: int = 20
    # OCR for scanned pages. Requires the Tesseract binary to be installed and
    # on PATH (or TESSDATA_PREFIX set). Off by default.
    ocr_enabled: bool = False
    ocr_language: str = "eng"

    # --- Chunking (measured in tokens) ---
    # all-MiniLM-L6-v2 truncates input beyond 256 word-piece tokens, so chunks
    # must stay well below that or their tail is silently ignored when embedding.
    # 120/20 chosen with scripts/evaluate_rag.py: 180-token chunks mixed 2-3
    # topics per chunk, which diluted their embeddings (Recall@5 86.7% -> 93.3%).
    chunk_size_tokens: int = Field(default=120, ge=20, le=2000)
    chunk_overlap_tokens: int = Field(default=20, ge=0)

    # --- LLM (answer generation) ---
    # auto       -> "openai" if OPENAI_API_KEY is set, otherwise "extractive"
    # openai     -> any OpenAI-compatible Chat Completions API (OpenAI, Groq,
    #               Together, local Ollama...) via OPENAI_BASE_URL
    # extractive -> no LLM: answers by quoting the most relevant sentences
    llm_provider: Literal["auto", "openai", "extractive"] = "auto"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None  # e.g. http://localhost:11434/v1 for Ollama
    openai_model: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    llm_max_tokens: int = Field(default=700, ge=50, le=4000)
    llm_timeout_seconds: float = Field(default=30, gt=0)

    # --- RAG retrieval ---
    rag_top_k: int = Field(default=5, ge=1, le=20)
    # Chunks below this cosine similarity are treated as irrelevant. Model
    # specific: calibrated for all-MiniLM-L6-v2 (see docs/rag-pipeline.md).
    rag_min_score: float = Field(default=0.20, ge=-1, le=1)
    rag_max_context_tokens: int = Field(default=1800, ge=100)
    rag_hnsw_ef_search: int = Field(default=100, ge=10, le=1000)
    # vector -> semantic search only
    # hybrid -> semantic + PostgreSQL full-text search, merged with Reciprocal
    #           Rank Fusion (helps exact terms: codes, names, "Section 4.2")
    rag_retrieval_mode: Literal["vector", "hybrid"] = "hybrid"
    # Candidates fetched from EACH retriever before fusion / re-ranking.
    rag_candidate_pool: int = Field(default=10, ge=1, le=200)  # 10 vs 20: same eval quality, ~40% faster re-ranking
    rag_rrf_k: int = Field(default=60, ge=1)  # RRF constant (60 = value from the original paper)
    # Cross-encoder re-ranking of the candidates (second, more accurate pass).
    rag_rerank_enabled: bool = True
    rag_rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # With re-ranking, the cross-encoder is the relevance gate: candidates below
    # this score are dropped and the cosine RAG_MIN_SCORE is NOT applied (the
    # cross-encoder judges relevance far better than cosine similarity, and a
    # keyword hit with low cosine similarity can still reach it). Model specific
    # (ms-marco logits); calibrated with scripts/evaluate_rag.py.
    rag_rerank_min_score: float = -6.0

    # --- Rate limiting (fixed window, Redis) ---
    rate_limit_enabled: bool = True
    rate_limit_login_per_ip: int = 10          # per minute
    rate_limit_login_per_email: int = 10       # per 15 minutes (slows password guessing on one account)
    rate_limit_register_per_ip: int = 5        # per hour
    rate_limit_chat_per_user: int = 30         # per minute
    rate_limit_upload_per_user: int = 30       # per hour
    rate_limit_password_reset_per_ip: int = 10     # per hour
    rate_limit_password_reset_per_email: int = 3   # per hour (no inbox flooding)
    rate_limit_verification_email_per_user: int = 3  # per hour

    # --- Quotas per user (0 = unlimited; admins can override per user) ---
    quota_max_documents: int = Field(default=200, ge=0)
    quota_max_storage_mb: int = Field(default=1024, ge=0)
    quota_max_questions_per_day: int = Field(default=300, ge=0)
    # Deleted documents stay in the trash (restorable) this long, then are purged.
    trash_retention_days: int = Field(default=30, ge=1, le=365)

    # --- Caching (Redis) ---
    cache_enabled: bool = True
    cache_query_embedding_ttl: int = 24 * 3600
    cache_answer_ttl: int = 10 * 60
    # Semantic answer cache: also reuse the answer of a previous question that
    # MEANS the same ("how many vacation days do I get?" ~ "how many days of
    # vacation do I get?"). Threshold = cosine similarity of the two question
    # embeddings; model specific, calibrated for all-MiniLM-L6-v2 (see
    # cache_service). Too low serves answers to DIFFERENT questions.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    semantic_cache_max_entries: int = Field(default=50, ge=1, le=1000)  # per user + document scope

    # --- Observability ---
    slow_request_ms: int = 1500
    # GET /metrics (Prometheus). It is NOT routed by nginx (only /api/ and
    # /health/ are), so it is reachable only inside the Docker network. Set a
    # token to also require "Authorization: Bearer <token>".
    metrics_enabled: bool = True
    metrics_token: SecretStr | None = None

    # --- Security ---
    hsts_enabled: bool = False  # set true only when served over HTTPS
    # Swagger UI / OpenAPI schema. Default: on in development, off in production.
    api_docs_enabled: bool | None = None

    @property
    def docs_enabled(self) -> bool:
        return self.api_docs_enabled if self.api_docs_enabled is not None else self.environment != "production"

    @field_validator("openai_api_key", "openai_base_url", "smtp_username", "smtp_password", "metrics_token",
                     mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Docker Compose passes unset optional variables as "" - treat as not set."""
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider == "auto":
            return "openai" if self.openai_api_key and self.openai_api_key.get_secret_value() else "extractive"
        return self.llm_provider

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_SIZE_TOKENS")
        if self.processing_soft_time_limit >= self.processing_time_limit:
            raise ValueError("PROCESSING_SOFT_TIME_LIMIT must be smaller than PROCESSING_TIME_LIMIT")
        if self.llm_provider == "openai" and not (self.openai_api_key and self.openai_api_key.get_secret_value()):
            if not self.openai_base_url:  # local OpenAI-compatible servers may not need a key
                raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY (or OPENAI_BASE_URL for a local server)")
        if self.environment == "production":
            self._check_production()
        if self.processing_stale_after <= self.processing_time_limit:
            # Otherwise a slow-but-alive task could be "stolen" by a second worker.
            raise ValueError("PROCESSING_STALE_AFTER must be larger than PROCESSING_TIME_LIMIT")
        return self

    def _check_production(self) -> None:
        """Refuse to START in production with development defaults - a
        misconfiguration should fail loudly at deploy time, not be exploited later."""
        problems = []
        if "documind_dev_password" in self.database_url:
            problems.append("DATABASE_URL uses the development password")
        if self.debug:
            problems.append("DEBUG must be false")
        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must list explicit origins, not '*'")
        secret = self.jwt_secret_key.get_secret_value()
        if len(set(secret)) < 10 or "change" in secret.lower() or "test" in secret.lower():
            problems.append("JWT_SECRET_KEY looks like a placeholder; generate a random one")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    # --- Database ---
    # Local dev default matches docker-compose.yml. Override in .env / production.
    database_url: str = "postgresql+psycopg://documind:documind_dev_password@localhost:5432/documind"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_echo: bool = False  # True = print every SQL statement (debugging only)

    # --- Redis / Celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None      # defaults to redis_url
    celery_result_backend: str | None = None  # defaults to redis db 1
    # Hard limit kills a stuck task; the soft limit fires first so the task can
    # record a clean "failed" status.
    processing_soft_time_limit: int = Field(default=15 * 60, ge=10)
    processing_time_limit: int = Field(default=20 * 60, ge=20)
    processing_max_retries: int = Field(default=3, ge=0, le=10)
    processing_retry_backoff: int = Field(default=10, ge=1)  # seconds, doubles each retry
    # A document stuck in "processing" longer than this is considered abandoned
    # (worker crashed / was killed) and may be claimed again.
    processing_stale_after: int = Field(default=30 * 60, ge=60)
    # Live status stream (GET /documents/events) closes after this many seconds;
    # the browser reconnects. Bounded so no connection (or expired login) lives forever.
    document_events_max_seconds: float = Field(default=600, gt=0, le=3600)

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        if self.celery_result_backend:
            return self.celery_result_backend
        base, _, _db = self.redis_url.rpartition("/")
        return f"{base}/1" if _db.isdigit() else self.redis_url

    # --- Authentication ---
    # No default on purpose: the app refuses to start without a secret.
    # SecretStr hides the value in repr()/logs.
    jwt_secret_key: SecretStr = Field(min_length=32)
    # Max. Argon2 hashes computed at the same time (0 = number of CPU cores).
    password_hash_concurrency: int = Field(default=0, ge=0, le=256)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "documind-ai"
    # Short-lived access token (kept only in memory by the web app) + a
    # long-lived, rotating refresh token in an httpOnly cookie.
    access_token_expire_minutes: int = Field(default=15, ge=1, le=24 * 60)
    refresh_token_expire_days: int = Field(default=14, ge=1, le=365)
    refresh_cookie_name: str = "documind_refresh"
    # Secure cookies are only sent over HTTPS. Default: on in production.
    refresh_cookie_secure: bool | None = None
    # Two browser tabs may refresh at the same moment with the same token: a
    # second use within this window is a race, not token theft.
    refresh_reuse_grace_seconds: int = Field(default=10, ge=0, le=300)

    # --- Email (verification + password reset) ---
    # console -> emails are written to the log (development; links visible in logs)
    # smtp    -> sent through SMTP_HOST (Mailpit in docker-compose, a real provider in production)
    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False  # true for port 587 on real providers
    smtp_timeout_seconds: float = Field(default=10, gt=0)
    email_from: str = "DocuMind AI <no-reply@documind.local>"
    # Links in emails point to the web app.
    frontend_base_url: str = "http://localhost:5173"
    email_verification_ttl_hours: int = Field(default=48, ge=1, le=24 * 30)
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=24 * 60)
    # true = an unverified account cannot log in. Off by default so existing
    # accounts keep working; the web app nudges unverified users instead.
    require_email_verification: bool = False

    @property
    def refresh_cookie_secure_effective(self) -> bool:
        if self.refresh_cookie_secure is not None:
            return self.refresh_cookie_secure
        return self.environment == "production"

    # --- Embeddings ---
    # Must match the model: all-MiniLM-L6-v2 -> 384 dims.
    # Changing this requires a new migration (vector column size is fixed).
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimension: int = 384
    embedding_batch_size: int = Field(default=32, ge=1, le=512)
    # Load the model in the background when the API starts (first question fast).
    preload_embedding_model: bool = True
    embedding_device: str = "cpu"  # "cuda" on a GPU machine
    # Some models (e5, bge) are trained with instruction prefixes such as
    # "query: " / "passage: ". MiniLM uses none.
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read env only once)."""
    return Settings()
