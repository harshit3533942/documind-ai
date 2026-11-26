from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, str_enum_column

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.document import Document


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # unique=True creates a UNIQUE index -> fast login lookup + no duplicate accounts.
    # Emails are normalised to lowercase in the service layer before saving.
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        str_enum_column(UserRole, "user_role"), default=UserRole.USER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true(), nullable=False)

    # One user -> many documents / conversations.
    # passive_deletes=True: let PostgreSQL's ON DELETE CASCADE do the work
    # instead of SQLAlchemy loading every child row into memory first.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # never include password hash in repr/logs
        return f"<User id={self.id} email={self.email} role={self.role}>"
