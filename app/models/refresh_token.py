import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One long-lived refresh token (one per login session and rotation step).

    * Only the SHA-256 HASH of the token is stored: a leaked database cannot
      be used to log in. (A fast hash is fine here - unlike passwords, the
      token is 256 random bits, so brute-forcing the hash is hopeless.)
    * family_id groups all tokens produced by rotation from ONE login.
      Rotation: every /auth/refresh revokes the presented token and issues a
      new one. If an already-revoked token is presented again, someone else
      (an attacker who stole it) used it -> the whole family is revoked.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when revoked by ROTATION (not by logout): only a rotated token gets the
    # short grace window for concurrent refreshes from two tabs.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),  # cleanup job
    )

    def __repr__(self) -> str:  # never include the hash
        return f"<RefreshToken id={self.id} user={self.user_id} family={self.family_id}>"
