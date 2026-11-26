"""Reusable FastAPI dependencies: current user + role-based access control."""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import PermissionDeniedError, UnauthorizedError
from app.core.security import TokenError, decode_access_token
from app.database.connection import get_db
from app.models import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.embedding_service import Embedder, get_embedder
from app.services.llm_service import LLMClient, get_llm_client
from app.services.retrieval_service import Reranker
from app.services.storage_service import LocalStorage
from app.services.task_queue import CeleryTaskQueue, TaskQueue

# tokenUrl powers the "Authorize" button in Swagger UI (/docs).
# auto_error=False -> we raise our own consistent 401 error format.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{get_settings().api_v1_prefix}/auth/login", auto_error=False
)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(db: DbSession, token: Annotated[str | None, Depends(oauth2_scheme)]) -> User:
    if not token:
        raise UnauthorizedError("Not authenticated")
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError):
        raise UnauthorizedError("Invalid or expired token") from None

    # Always re-load from the DB: a deleted/disabled user or a demoted admin
    # loses access immediately, even if their token has not expired yet.
    user = UserRepository(db).get_by_id(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[[User], User]:
    """Dependency factory: `Depends(require_roles(UserRole.ADMIN))`."""

    def checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise PermissionDeniedError("You do not have permission to perform this action")
        return user

    return checker


AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]


def get_storage() -> LocalStorage:
    return LocalStorage(get_settings().storage_dir)


Storage = Annotated[LocalStorage, Depends(get_storage)]


_celery_queue = CeleryTaskQueue()


def get_task_queue() -> TaskQueue:
    return _celery_queue


Queue = Annotated[TaskQueue, Depends(get_task_queue)]


_cached_embedder = None


def get_embedder_dependency() -> Embedder:
    """Process-wide singleton model, with Redis caching of query embeddings."""
    global _cached_embedder
    if _cached_embedder is None:
        from app.services.cache_service import CachedEmbedder

        _cached_embedder = CachedEmbedder(get_embedder())
    return _cached_embedder


def get_llm_dependency() -> LLMClient | None:
    return get_llm_client()  # None -> extractive mode


def get_reranker_dependency() -> Reranker | None:
    from app.services.rerank_service import get_reranker

    return get_reranker()  # None -> re-ranking disabled


EmbedderDep = Annotated[Embedder, Depends(get_embedder_dependency)]
RerankerDep = Annotated[Reranker | None, Depends(get_reranker_dependency)]
LLMDep = Annotated[LLMClient | None, Depends(get_llm_dependency)]
