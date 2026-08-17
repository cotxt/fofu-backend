from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminLoginRequest(AdminModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().casefold()


class AdminOverviewResponse(AdminModel):
    users_total: int = Field(ge=0)
    users_active: int = Field(ge=0)
    restaurants_total: int = Field(ge=0)
    restaurants_published: int = Field(ge=0)
    owner_applications_pending: int = Field(ge=0)
    owner_applications_under_review: int = Field(ge=0)
    audit_events_total: int = Field(ge=0)


class AdminUserResponse(AdminModel):
    id: str
    email: EmailStr | None
    display_name: str
    locale: str
    is_guest: bool
    is_active: bool
    roles: list[str] = Field(default_factory=list)
    created_at: datetime


class AdminUserListResponse(AdminModel):
    items: list[AdminUserResponse]
    total: int = Field(ge=0)


class AdminRestaurantResponse(AdminModel):
    id: str
    slug: str
    name_en: str
    name_ko: str | None
    owner_user_id: str | None
    is_verified: bool
    is_published: bool
    is_open: bool
    created_at: datetime


class AdminRestaurantListResponse(AdminModel):
    items: list[AdminRestaurantResponse]
    total: int = Field(ge=0)


class AdminRestaurantModerationUpdate(AdminModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    is_published: bool | None = Field(
        default=None,
        description="Whether the restaurant is visible in the public catalog.",
    )
    is_verified: bool | None = Field(
        default=None,
        description="Whether the restaurant information has been verified.",
    )
    is_open: bool | None = Field(
        default=None,
        description="Whether the restaurant is currently accepting orders.",
    )

    @model_validator(mode="after")
    def require_moderation_field(self) -> AdminRestaurantModerationUpdate:
        moderation_fields = {"is_published", "is_verified", "is_open"}
        provided_fields = self.model_fields_set & moderation_fields
        if not provided_fields:
            raise ValueError("At least one restaurant moderation field is required")
        if any(getattr(self, field) is None for field in provided_fields):
            raise ValueError("Restaurant moderation fields cannot be null")
        return self


class AdminOwnerApplicationResponse(AdminModel):
    id: str
    applicant_user_id: str
    applicant_email: EmailStr | None
    applicant_display_name: str
    restaurant_id: str | None
    restaurant_name: str | None
    business_name: str
    registration_number: str
    address: str
    phone: str
    license_media_id: str
    license_original_filename: str
    license_content_type: str
    agreed_to_terms_at: datetime
    terms_version: str
    phone_verified_at: datetime | None
    status: str
    review_note: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminOwnerApplicationListResponse(AdminModel):
    items: list[AdminOwnerApplicationResponse]
    total: int = Field(ge=0)


class AdminOwnerApplicationReview(AdminModel):
    status: Literal["under_review", "approved", "rejected"]
    review_note: str | None = Field(default=None, max_length=2_000)
    restaurant_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("review_note")
    @classmethod
    def normalize_review_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def require_rejection_note(self) -> AdminOwnerApplicationReview:
        if self.status == "rejected" and self.review_note is None:
            raise ValueError("A review note is required when rejecting an application")
        return self


class AdminAuditEventResponse(AdminModel):
    id: str
    actor_user_id: str | None
    actor_email: EmailStr | None
    action: str
    resource_type: str
    resource_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdminAuditEventListResponse(AdminModel):
    items: list[AdminAuditEventResponse]
    total: int = Field(ge=0)
