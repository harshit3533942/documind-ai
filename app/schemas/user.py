"""Pydantic schemas = the API contract.

Separate *input* and *output* models: UserCreate accepts a password, UserRead
never contains it. Returning ORM objects directly risks leaking
hashed_password - response_model=UserRead makes that impossible.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole


def check_password_strength(v: str) -> str:
    if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
        raise ValueError("Password must contain at least one letter and one digit")
    return v


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    # max_length guards against "long password" DoS (hashing 1 MB strings is slow).
    password: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return check_password_strength(v)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build from ORM objects

    id: uuid.UUID
    name: str
    email: EmailStr
    role: UserRole
    is_active: bool
    email_verified_at: datetime | None = None
    created_at: datetime
