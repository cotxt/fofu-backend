from __future__ import annotations

import re
from datetime import datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MediaAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    purpose: str
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    created_at: datetime


class OwnerApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restaurant_id: str = Field(min_length=1, max_length=36)
    business_name: str = Field(min_length=1, max_length=200)
    registration_number: str = Field(min_length=3, max_length=30)
    address: str = Field(min_length=3, max_length=300)
    phone: str = Field(min_length=5, max_length=30)
    license_media_id: str = Field(min_length=1, max_length=36)
    agreed_to_terms: bool
    terms_version: str = Field(min_length=1, max_length=30)

    @field_validator("business_name", "address")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("registration_number")
    @classmethod
    def normalize_registration_number(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .\-/]{1,28}[A-Z0-9]", value):
            raise ValueError("registration_number contains invalid characters")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\+?[0-9][0-9 ()-]{3,28}[0-9]", value):
            raise ValueError("phone contains invalid characters")
        return value

    @field_validator("terms_version")
    @classmethod
    def normalize_terms_version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,29}", value):
            raise ValueError("terms_version contains invalid characters")
        return value

    @model_validator(mode="after")
    def require_terms(self) -> OwnerApplicationCreate:
        if not self.agreed_to_terms:
            raise ValueError("Merchant Terms must be accepted")
        return self


class OwnerApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    applicant_user_id: str
    restaurant_id: str | None
    business_name: str
    registration_number: str
    address: str
    phone: str
    license_media_id: str
    agreed_to_terms_at: datetime
    terms_version: str
    phone_verified_at: datetime | None
    status: str
    review_note: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OwnerApplicationListResponse(BaseModel):
    items: list[OwnerApplicationResponse]


class OwnerRestaurantSummary(BaseModel):
    id: str
    slug: str
    name_en: str
    name_ko: str | None
    category: str
    address_en: str
    address_ko: str | None
    cover_image_url: str | None
    rating_avg: float
    rating_count: int
    is_verified: bool
    is_open: bool
    is_published: bool
    menu_revision: int


class OwnerRestaurantListResponse(BaseModel):
    items: list[OwnerRestaurantSummary]


class OpeningHourResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    day_of_week: int
    opens_at: time | None
    closes_at: time | None
    is_closed: bool


class OwnerMenuItemResponse(BaseModel):
    id: str
    category_id: str
    slug: str
    name_en: str
    name_ko: str | None
    price_amount: int | None
    currency: str
    image_url: str | None
    is_available: bool
    sort_order: int


class OwnerDashboardResponse(BaseModel):
    restaurant: OwnerRestaurantSummary
    membership_role: str
    hours: list[OpeningHourResponse]
    menu_items: list[OwnerMenuItemResponse]
    menu_item_count: int
    photo_count: int
    published_review_count: int
    new_review_count: int
    weekly_views: int = Field(
        ge=0,
        description="Tracked QR scans for this restaurant during the last seven days.",
    )


class OpenStatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_open: bool


class OpeningHourPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: int = Field(ge=0, le=6)
    opens_at: time | None = None
    closes_at: time | None = None
    is_closed: bool = False

    @model_validator(mode="after")
    def validate_times(self) -> OpeningHourPatch:
        if self.is_closed:
            if self.opens_at is not None or self.closes_at is not None:
                raise ValueError("Closed days cannot include opening or closing times")
        elif self.opens_at is None or self.closes_at is None:
            raise ValueError("Open days require both opens_at and closes_at")
        elif self.opens_at == self.closes_at:
            raise ValueError("opens_at and closes_at cannot be equal")
        return self


class OpeningHoursPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: list[OpeningHourPatch] = Field(min_length=1, max_length=7)

    @field_validator("hours")
    @classmethod
    def unique_days(cls, values: list[OpeningHourPatch]) -> list[OpeningHourPatch]:
        days = [value.day_of_week for value in values]
        if len(set(days)) != len(days):
            raise ValueError("Each day_of_week may appear only once")
        return values


class MenuAvailabilityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_available: bool
