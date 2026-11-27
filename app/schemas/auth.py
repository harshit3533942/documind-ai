from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.user import check_password_strength


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class EmailTokenRequest(BaseModel):
    """The token from the link in an email (verification)."""

    token: str = Field(min_length=10, max_length=200)


class PasswordResetRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.strip().lower()


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return check_password_strength(v)


class MessageResponse(BaseModel):
    message: str
