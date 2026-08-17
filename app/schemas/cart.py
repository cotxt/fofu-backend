from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.catalog import Money

ServingMode = Literal["dine_in", "takeout"]
CompatibilityStatus = Literal["compatible", "conflict", "unknown"]


class CartModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CartItemUpsert(CartModel):
    quantity: int = Field(ge=1, le=99)
    notes: str | None = Field(default=None, max_length=500)
    request_codes: list[str] = Field(default_factory=list, max_length=12)
    expected_version: int | None = Field(default=None, ge=1)
    expected_menu_revision: int | None = Field(default=None, ge=1)

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value else None
        return cleaned or None

    @field_validator("request_codes")
    @classmethod
    def normalize_request_codes(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            code = item.strip().lower().replace("-", "_")
            if not code or len(code) > 60 or not code.replace("_", "").isalnum():
                raise ValueError("request_codes must be short alphanumeric identifiers")
            if code not in normalized:
                normalized.append(code)
        return normalized


class CartSettingsUpdate(CartModel):
    serving_mode: ServingMode | None = None
    expected_version: int | None = Field(default=None, ge=1)
    expected_menu_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change(self) -> CartSettingsUpdate:
        if self.serving_mode is None:
            raise ValueError("At least one cart setting must be supplied")
        return self


class CartItem(CartModel):
    id: str
    menu_item_id: str
    name: str
    original_name: str | None = None
    quantity: int = Field(ge=1)
    unit_price: Money | None
    line_total: Money | None
    notes: str | None = None
    request_codes: list[str] = Field(default_factory=list)
    is_available: bool


class Cart(CartModel):
    id: str
    restaurant_id: str
    restaurant_name: str
    status: str
    serving_mode: ServingMode
    table_label: str | None = None
    menu_revision: int = Field(ge=1)
    version: int = Field(ge=1)
    items: list[CartItem] = Field(default_factory=list)
    item_count: int = Field(ge=0)
    subtotal: Money | None
    updated_at: datetime


class CompatibilityReason(CartModel):
    code: str
    kind: Literal["allergen", "ingredient", "diet", "data_quality"]
    message: str
    menu_item_id: str | None = None
    relationship: str | None = None


class CompatibilityAssessment(CartModel):
    status: CompatibilityStatus
    reasons: list[CompatibilityReason] = Field(default_factory=list)
    disclaimer: str


class OrderCardItem(CartModel):
    menu_item_id: str
    name: str
    original_name: str
    quantity: int = Field(ge=1)
    unit_price: Money | None
    line_total: Money | None
    notes: str | None = None
    request_codes: list[str] = Field(default_factory=list)


class OrderCard(CartModel):
    order_id: str | None = None
    status: Literal["preview", "prepared"] = "preview"
    cart_id: str
    cart_version: int = Field(ge=1)
    menu_revision: int = Field(ge=1)
    restaurant_id: str
    restaurant_name: str
    table_label: str | None = None
    serving_mode: ServingMode
    items: list[OrderCardItem]
    subtotal: Money | None
    total: Money | None
    korean_phrase: str
    translated_phrase: str
    request_note_ko: str | None = None
    request_note_localized: str | None = None
    compatibility: CompatibilityAssessment
    generated_at: datetime


class OrderCardPrepareRequest(CartModel):
    idempotency_key: str = Field(min_length=8, max_length=100)
    expected_version: int | None = Field(default=None, ge=1)
    expected_menu_revision: int | None = Field(default=None, ge=1)
    locale: str | None = Field(default=None, min_length=2, max_length=35)
