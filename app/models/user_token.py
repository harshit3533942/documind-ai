import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import TokenPurpose, str_enum_column


class UserToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Single-use token sent by email: verify an address or reset a password.

    * Only the SHA-256 hash is stored (the link in the email is the secret).
    * Single use (`used_at`) and short-lived (`expires_at`).
    * Requesting a new token invalidates the older unused ones of the same
      purpose, so only the most recent email works.
    """

    __tablename__ = "user_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[TokenPurpose] = mapped_column(str_enum_column(TokenPurpose, "token_purpose"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_user_tokens_user_id_purpose", "user_id", "purpose"),
        Index("ix_user_tokens_expires_at", "expires_at"),  # cleanup job
    )

    def __repr__(self) -> str:  # never include the hash
        return f"<UserToken id={self.id} user={self.user_id} purpose={self.purpose}>"
