from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PushDeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=2, max_length=512, pattern=r"^[0-9A-Fa-f]+$")
    environment: Literal["sandbox", "production"]
    locale: str | None = Field(default=None, min_length=2, max_length=35)

    @field_validator("token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        if len(value) % 2 != 0:
            raise ValueError("token must contain an even number of hexadecimal characters")
        return value.lower()

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("_", "-")
        return normalized or None


class PushDeviceResponse(BaseModel):
    installation_id: str
    platform: Literal["ios"] = "ios"
    environment: Literal["sandbox", "production"]
    topic: str
    locale: str | None
    is_active: bool
    last_registered_at: datetime
