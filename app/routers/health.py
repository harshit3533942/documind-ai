"""Health-check endpoints.

- /health/live  : is the process up? (liveness probe - never checks dependencies,
                  otherwise a DB outage would make the orchestrator restart
                  perfectly healthy API containers)
- /health/ready : can we serve traffic right now? (checks the database)
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.database.connection import check_database_connection
from app.database.redis_client import check_redis_connection

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness() -> JSONResponse:
    settings = get_settings()
    # Both clients are synchronous - run them in threads so the event loop stays free.
    database = await run_in_threadpool(check_database_connection)
    redis_status = await run_in_threadpool(check_redis_connection)
    healthy = database["status"] == "ok" and redis_status["status"] == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "timestamp": datetime.now(UTC).isoformat(),
            "checks": {"database": database, "redis": redis_status},
        },
    )
