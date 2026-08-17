from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, Field, model_validator


def _as_utc(value: datetime) -> datetime:
    """Normalize database datetimes for unambiguous RFC 3339 API output.

    SQLite drops timezone metadata even for ``DateTime(timezone=True)`` columns.
    Datetimes written by this service are UTC, so a naive value read back from
    SQLite represents UTC rather than local server time.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class Money(BaseModel):
    amount: int = Field(ge=0, description="Minor currency units; KRW has no decimal subdivision.")
    currency: str = Field(min_length=3, max_length=3, examples=["KRW"])
    formatted: str = ""

    @model_validator(mode="after")
    def fill_display_value(self) -> Money:
        if not self.formatted:
            self.formatted = (
                f"₩{self.amount:,}"
                if self.currency.upper() == "KRW"
                else f"{self.amount:,} {self.currency.upper()}"
            )
        return self


class APIErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class APIErrorResponse(BaseModel):
    error: APIErrorBody
    request_id: str


class MessageResponse(BaseModel):
    message: str


class TimestampedResponse(BaseModel):
    created_at: UTCDateTime
    updated_at: UTCDateTime


T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
