"""The logged-in user's own account: quota usage."""

from fastapi import APIRouter

from app.core.dependencies import CurrentUser, DbSession
from app.schemas.account import QuotaSummary
from app.services import quota_service

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/usage", response_model=QuotaSummary)
def my_usage(user: CurrentUser, db: DbSession) -> QuotaSummary:
    """What you use vs. your limits (0 = unlimited). Documents in the trash
    count toward documents and storage until they are deleted for good."""
    return QuotaSummary(**quota_service.summary(db, user))
