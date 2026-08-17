from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.auth import ClientType, TokenBundle, UserSummary
from app.schemas.cart import Cart
from app.schemas.catalog import Money

QRPurpose = Literal["restaurant_entry", "table_entry", "menu"]


class QRModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QRGuestSessionRequest(QRModel):
    code: str = Field(min_length=16, max_length=256)
    locale: str = Field(default="en", min_length=2, max_length=35)
    client_type: ClientType = "web"
    device_id: str | None = Field(default=None, max_length=255)


class QRRestaurantContext(QRModel):
    id: str
    slug: str
    name: str
    original_name: str | None = None
    description: str
    address: str
    original_address: str | None = None
    category: str
    phone: str | None = None
    currency: str
    timezone: str
    is_verified: bool
    is_open: bool
    cover_image_url: str | None = None
    menu_revision: int = Field(ge=1)


class QRMenuItem(QRModel):
    id: str
    category_id: str
    slug: str
    name: str
    original_name: str | None = None
    pronunciation: str | None = None
    description: str
    price: Money | None = None
    serving_description: str | None = None
    spice_level: int = Field(ge=0)
    badge: str | None = None
    image_url: str | None = None
    is_available: bool
    is_orderable: bool
    orderability_reason: str | None = None
    ingredient_codes: list[str] = Field(default_factory=list)
    allergen_codes: list[str] = Field(default_factory=list)
    dietary_claims: list[str] = Field(default_factory=list)


class QRMenuCategory(QRModel):
    id: str
    slug: str
    name: str
    sort_order: int
    items: list[QRMenuItem] = Field(default_factory=list)


class QRSessionContext(QRModel):
    id: str
    scope: str
    restaurant_id: str
    qr_code_id: str | None = None
    qr_purpose: str | None = None
    table_label: str | None = None
    allowed_serving_modes: list[Literal["dine_in", "takeout"]]
    expires_at: datetime


class SessionBootstrap(QRModel):
    session: QRSessionContext
    user: UserSummary
    restaurant: QRRestaurantContext
    menu: list[QRMenuCategory]
    cart: Cart


class QRGuestSessionResponse(TokenBundle):
    user: UserSummary
    bootstrap: SessionBootstrap


class QRResolveResponse(QRModel):
    restaurant_id: str
    restaurant_slug: str
    restaurant_name: str
    table_label: str | None = None
    purpose: str
    menu_revision: int = Field(ge=1)
    web_url: str


class OwnerQRCodeCreate(QRModel):
    label: str | None = Field(default=None, max_length=100)
    table_label: str | None = Field(default=None, max_length=50)
    purpose: QRPurpose = "restaurant_entry"
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_context(self) -> OwnerQRCodeCreate:
        if self.purpose == "table_entry" and not self.table_label:
            raise ValueError("table_label is required for a table_entry QR code")
        return self


class OwnerQRCodeUpdate(QRModel):
    label: str | None = Field(default=None, max_length=100)
    table_label: str | None = Field(default=None, max_length=50)
    purpose: QRPurpose | None = None
    is_active: bool | None = None
    expires_at: datetime | None = None
    menu_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change(self) -> OwnerQRCodeUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one QR code field must be supplied")
        if self.purpose == "table_entry" and "table_label" in self.model_fields_set:
            if not self.table_label:
                raise ValueError("table_label is required for a table_entry QR code")
        return self


class OwnerQRCode(QRModel):
    id: str
    public_hint: str
    restaurant_id: str
    label: str | None = None
    table_label: str | None = None
    purpose: str
    menu_revision: int = Field(ge=1)
    is_active: bool
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OwnerQRCodeCreated(OwnerQRCode):
    code: str
    web_url: str
    svg_url: str


class OwnerQRCodeList(QRModel):
    items: list[OwnerQRCode] = Field(default_factory=list)


class QRCodeRevoked(QRModel):
    id: str
    revoked: bool
