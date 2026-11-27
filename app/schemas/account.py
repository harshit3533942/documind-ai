from pydantic import BaseModel, Field


class QuotaLimits(BaseModel):
    max_documents: int
    max_storage_bytes: int
    max_questions_per_day: int


class QuotaUsage(BaseModel):
    documents: int
    storage_bytes: int
    trashed_documents: int
    questions_today: int


class QuotaSummary(BaseModel):
    """0 in a limit = unlimited."""

    limits: QuotaLimits
    usage: QuotaUsage


class QuotaUpdate(BaseModel):
    """Admin override for one user. null = back to the default from settings,
    0 = unlimited. Fields that are not sent are left unchanged."""

    max_documents: int | None = Field(default=None, ge=0, le=1_000_000)
    max_storage_mb: int | None = Field(default=None, ge=0, le=10_000_000)
    max_questions_per_day: int | None = Field(default=None, ge=0, le=1_000_000)
