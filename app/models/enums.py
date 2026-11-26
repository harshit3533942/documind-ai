"""Enumerations shared by models, schemas and services."""

from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


def str_enum_column(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Store enums as VARCHAR + CHECK constraint instead of a native PG ENUM.

    Native PG enums need ALTER TYPE migrations to add a value; a CHECK
    constraint is easier to evolve and still rejects invalid values.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
