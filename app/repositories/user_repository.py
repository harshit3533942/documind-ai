"""Data access for users. Only this layer writes SQL for the `users` table."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User, UserRole


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email.lower()))

    def create(self, *, name: str, email: str, hashed_password: str, role: UserRole = UserRole.USER) -> User:
        user = User(name=name, email=email.lower(), hashed_password=hashed_password, role=role)
        self.db.add(user)
        self.db.flush()  # get id/defaults now; caller decides when to commit
        return user

    def list_users(self, *, limit: int, offset: int) -> tuple[list[User], int]:
        total = self.db.scalar(select(func.count()).select_from(User)) or 0
        users = self.db.scalars(
            select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(users), total
