from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import UTCDateTime
from app.utils import SUPPORTED_LOCALES

_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
_LOCALE_BY_LOWER = {locale.lower(): locale for locale in SUPPORTED_LOCALES}


def _normalize_codes(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip().lower()
        if not _CODE_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid food preference code: {raw!r}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str | None
    display_name: str
    home_country_code: str | None
    locale: str
    is_guest: bool
    roles: list[str]
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    home_country_code: str | None = Field(default=None, min_length=2, max_length=2)
    locale: str | None = Field(default=None, min_length=2, max_length=35)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("display_name cannot be null")
        value = " ".join(value.split())
        if not value:
            raise ValueError("display_name cannot be blank")
        return value

    @field_validator("home_country_code")
    @classmethod
    def normalize_country_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("home_country_code must be an ISO 3166-1 alpha-2 code")
        return value

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("locale cannot be null")
        candidate = value.strip().replace("_", "-")
        normalized = _LOCALE_BY_LOWER.get(candidate.lower())
        if normalized is None:
            raise ValueError("Unsupported locale")
        return normalized

    @model_validator(mode="after")
    def require_field(self) -> ProfilePatch:
        if not self.model_fields_set:
            raise ValueError("At least one profile field is required")
        return self


class PassportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    diet_codes: list[str]
    avoid_allergen_codes: list[str]
    avoid_ingredient_codes: list[str]
    liked_ingredient_codes: list[str]
    spice_tolerance: int
    avoidance_details: list[dict[str, Any]]
    disliked_textures: list[str]
    learned_preferences: dict[str, Any]
    version: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class PassportPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diet_codes: list[str] | None = Field(default=None, max_length=50)
    avoid_allergen_codes: list[str] | None = Field(default=None, max_length=50)
    avoid_ingredient_codes: list[str] | None = Field(default=None, max_length=100)
    liked_ingredient_codes: list[str] | None = Field(default=None, max_length=100)
    spice_tolerance: int | None = Field(default=None, ge=0, le=5)
    avoidance_details: list[dict[str, Any]] | None = Field(default=None, max_length=50)
    disliked_textures: list[str] | None = Field(default=None, max_length=50)
    version: int | None = Field(
        default=None,
        ge=0,
        description="Last passport version observed by the client (0 when creating).",
    )

    @field_validator(
        "diet_codes",
        "avoid_allergen_codes",
        "avoid_ingredient_codes",
        "liked_ingredient_codes",
    )
    @classmethod
    def normalize_codes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            raise ValueError("Passport lists cannot be null; use an empty list to clear them")
        return _normalize_codes(value)

    @field_validator("spice_tolerance")
    @classmethod
    def reject_null_spice_tolerance(cls, value: int | None) -> int | None:
        if value is None:
            raise ValueError("spice_tolerance cannot be null")
        return value

    @field_validator("avoidance_details")
    @classmethod
    def validate_avoidance_details(
        cls, value: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if value is None:
            raise ValueError("avoidance_details cannot be null; use an empty list to clear it")
        for detail in value:
            if not detail or len(detail) > 12:
                raise ValueError("Each avoidance detail must contain between 1 and 12 fields")
            for key, item in detail.items():
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                    raise ValueError("Avoidance detail keys must be lower_snake_case")
                if not isinstance(item, (str, int, float, bool, type(None))):
                    raise ValueError("Avoidance detail values must be scalar JSON values")
                if isinstance(item, str) and len(item) > 500:
                    raise ValueError("Avoidance detail text is too long")
            code = detail.get("code")
            if not isinstance(code, str):
                raise ValueError("Each avoidance detail requires a string code")
            normalized_code = code.strip().lower()
            if not _CODE_PATTERN.fullmatch(normalized_code):
                raise ValueError("Avoidance detail code is invalid")
            detail["code"] = normalized_code
        return value

    @field_validator("disliked_textures")
    @classmethod
    def normalize_textures(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            raise ValueError("disliked_textures cannot be null; use an empty list to clear it")
        return _normalize_codes(value)

    @model_validator(mode="after")
    def require_field(self) -> PassportPatch:
        if not (self.model_fields_set - {"version"}):
            raise ValueError("At least one passport field is required")
        return self


class SavedRestaurantResponse(BaseModel):
    id: str
    slug: str
    name: str
    name_en: str
    name_ko: str | None
    category: str
    address: str
    address_en: str
    address_ko: str | None
    latitude: float
    longitude: float
    rating_avg: float
    rating_count: int
    is_verified: bool
    is_open: bool
    cover_image_url: str | None
    saved_at: UTCDateTime


class SavedRestaurantListResponse(BaseModel):
    items: list[SavedRestaurantResponse]
    next_cursor: str | None = None
    has_more: bool = False


class OrderHistoryItemResponse(BaseModel):
    id: str
    menu_item_id: str | None
    name: str
    name_en_snapshot: str
    name_ko_snapshot: str
    unit_price_amount: int | None
    line_total_amount: int | None
    quantity: int
    notes: str | None


class OrderHistoryResponse(BaseModel):
    id: str
    restaurant_id: str
    restaurant_slug: str
    restaurant_name: str
    status: str
    serving_mode: str
    table_label: str | None = None
    subtotal_amount: int | None
    total_amount: int | None
    currency: str
    korean_phrase: str
    translated_phrase: str
    allergy_note_ko: str | None
    allergy_note_localized: str | None
    items: list[OrderHistoryItemResponse]
    item_count: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class OrderHistoryListResponse(BaseModel):
    items: list[OrderHistoryResponse]
    next_cursor: str | None = None
    has_more: bool = False
