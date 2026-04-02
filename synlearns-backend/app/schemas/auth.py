from pydantic import BaseModel, EmailStr, Field
from uuid import UUID
from datetime import datetime


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=1024)
    device_fingerprint: str = Field(min_length=16, max_length=128)


class RegisterRequest(BaseModel):
    email: EmailStr
    activation_token: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=1024)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    device_fingerprint: str = Field(min_length=16, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserPublic"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(max_length=4096)
    device_fingerprint: str = Field(min_length=16, max_length=128)


class UserPublic(BaseModel):
    id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    tier: int
    account_status: str
    expires_at: datetime | None
    is_admin: bool

    class Config:
        from_attributes = True
