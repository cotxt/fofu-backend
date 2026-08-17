from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.common import UTCDateTime

ClientType = Literal["web", "ios", "android"]


class AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnonymousRequest(AuthModel):
    locale: str = Field(default="en", min_length=2, max_length=35)
    display_name: str = Field(default="Guest", min_length=1, max_length=100)
    client_type: ClientType = "web"
    device_id: str | None = Field(default=None, max_length=255)


class RegisterRequest(AuthModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    locale: str = Field(default="en", min_length=2, max_length=35)
    home_country_code: str | None = Field(default=None, min_length=2, max_length=2)
    client_type: ClientType = "web"
    device_id: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().casefold()

    @field_validator("home_country_code")
    @classmethod
    def normalize_country_code(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class LoginRequest(AuthModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    client_type: ClientType = "web"
    device_id: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().casefold()


class GoogleLoginRequest(AuthModel):
    id_token: str = Field(min_length=1, max_length=8192)
    replaced_refresh_token: str | None = Field(default=None, min_length=32, max_length=512)
    locale: str = Field(default="en", min_length=2, max_length=35)
    client_type: ClientType = "web"
    device_id: str | None = Field(default=None, max_length=255)


class RefreshRequest(AuthModel):
    refresh_token: str | None = Field(default=None, min_length=32, max_length=512)


class LogoutRequest(AuthModel):
    refresh_token: str | None = Field(default=None, min_length=32, max_length=512)
    all_sessions: bool = False


class UserSummary(AuthModel):
    id: str
    email: EmailStr | None
    display_name: str
    home_country_code: str | None
    locale: str
    is_guest: bool
    roles: list[str] = Field(default_factory=list)


class TokenBundle(AuthModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0)
    refresh_token: str | None = None
    refresh_expires_at: UTCDateTime
    scope: str


class AuthResponse(TokenBundle):
    user: UserSummary


class LogoutResponse(AuthModel):
    revoked: bool
