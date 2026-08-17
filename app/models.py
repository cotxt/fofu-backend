from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(100), default="Guest")
    home_country_code: Mapped[str | None] = mapped_column(String(2))
    locale: Mapped[str] = mapped_column(String(35), default="en")
    is_guest: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)

    passport: Mapped[FoodPassport | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    identities: Mapped[list[AuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthIdentity(TimestampMixin, Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_auth_identities_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_auth_identities_user_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))

    user: Mapped[User] = relationship(back_populates="identities")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_type: Mapped[str] = mapped_column(String(20))
    device_id: Mapped[str | None] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(String(40), default="full")
    is_guest_at_issue: Mapped[bool] = mapped_column(Boolean, nullable=False)
    qr_restaurant_id: Mapped[str | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class PushDevice(TimestampMixin, Base):
    __tablename__ = "push_devices"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "topic",
            "environment",
            "installation_id",
            name="uq_push_device_installation",
        ),
        UniqueConstraint(
            "platform",
            "topic",
            "environment",
            "device_token",
            name="uq_push_device_token",
        ),
        Index("ix_push_devices_user_active", "user_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    auth_session_id: Mapped[str] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True
    )
    installation_id: Mapped[str] = mapped_column(String(128))
    device_token: Mapped[str] = mapped_column(String(512))
    platform: Mapped[str] = mapped_column(String(20), default="ios")
    topic: Mapped[str] = mapped_column(String(255))
    environment: Mapped[str] = mapped_column(String(20))
    locale: Mapped[str | None] = mapped_column(String(35))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(100))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PushDelivery(TimestampMixin, Base):
    __tablename__ = "push_deliveries"
    __table_args__ = (
        UniqueConstraint("event_key", "device_id", name="uq_push_delivery_event_device"),
        Index(
            "ix_push_deliveries_dispatch",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_push_delivery_attempts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(String(160))
    device_id: Mapped[str] = mapped_column(
        ForeignKey("push_devices.id", ondelete="CASCADE"), index=True
    )
    recipient_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    notification_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apns_id: Mapped[str | None] = mapped_column(String(36))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FoodPassport(TimestampMixin, Base):
    __tablename__ = "food_passports"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    diet_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    avoid_allergen_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    avoid_ingredient_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    liked_ingredient_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    spice_tolerance: Mapped[int] = mapped_column(Integer, default=2)
    avoidance_details: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    disliked_textures: Mapped[list[str]] = mapped_column(JSON, default=list)
    learned_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)

    user: Mapped[User] = relationship(back_populates="passport")


class Restaurant(TimestampMixin, Base):
    __tablename__ = "restaurants"
    __table_args__ = (
        Index(
            "ix_restaurants_published_latitude_longitude",
            "is_published",
            "latitude",
            "longitude",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    name_en: Mapped[str] = mapped_column(String(160))
    name_ko: Mapped[str | None] = mapped_column(String(160))
    description_en: Mapped[str] = mapped_column(Text, default="")
    description_ko: Mapped[str | None] = mapped_column(Text)
    handle: Mapped[str] = mapped_column(String(100), unique=True)
    category: Mapped[str] = mapped_column(String(80))
    hero_style: Mapped[str] = mapped_column(String(30), default="charcoal")
    address_en: Mapped[str] = mapped_column(String(300))
    address_ko: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(30))
    latitude: Mapped[float] = mapped_column(Float, index=True)
    longitude: Mapped[float] = mapped_column(Float, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    timezone_name: Mapped[str] = mapped_column(String(50), default="Asia/Seoul")
    rating_avg: Mapped[Decimal] = mapped_column(Numeric(2, 1), default=Decimal("0.0"))
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    menu_revision: Mapped[int] = mapped_column(Integer, default=1)
    cover_image_url: Mapped[str | None] = mapped_column(Text)
    gallery: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    hours: Mapped[list[OpeningHour]] = relationship(
        back_populates="restaurant",
        cascade="all, delete-orphan",
        order_by="OpeningHour.day_of_week",
    )
    menu_categories: Mapped[list[MenuCategory]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    translations: Mapped[list[RestaurantTranslation]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )


class RestaurantTranslation(Base):
    __tablename__ = "restaurant_translations"

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    address: Mapped[str] = mapped_column(String(300))

    restaurant: Mapped[Restaurant] = relationship(back_populates="translations")


class OpeningHour(Base):
    __tablename__ = "opening_hours"
    __table_args__ = (
        UniqueConstraint("restaurant_id", "day_of_week"),
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_opening_hour_day"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)

    restaurant: Mapped[Restaurant] = relationship(back_populates="hours")


class MenuCategory(TimestampMixin, Base):
    __tablename__ = "menu_categories"
    __table_args__ = (UniqueConstraint("restaurant_id", "slug"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(100))
    name_en: Mapped[str] = mapped_column(String(100))
    name_ko: Mapped[str | None] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    restaurant: Mapped[Restaurant] = relationship(back_populates="menu_categories")
    items: Mapped[list[MenuItem]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="MenuItem.sort_order"
    )
    translations: Mapped[list[MenuCategoryTranslation]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class MenuCategoryTranslation(Base):
    __tablename__ = "menu_category_translations"

    category_id: Mapped[str] = mapped_column(
        ForeignKey("menu_categories.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

    category: Mapped[MenuCategory] = relationship(back_populates="translations")


class MenuItem(TimestampMixin, Base):
    __tablename__ = "menu_items"
    __table_args__ = (
        UniqueConstraint("restaurant_id", "slug"),
        Index("ix_menu_items_restaurant_available", "restaurant_id", "is_available"),
        CheckConstraint("price_amount >= 0", name="ck_menu_item_price_nonnegative"),
        CheckConstraint("spice_level >= 0 AND spice_level <= 5", name="ck_menu_item_spice"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("menu_categories.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(120))
    name_en: Mapped[str] = mapped_column(String(160))
    name_ko: Mapped[str | None] = mapped_column(String(160))
    pronunciation: Mapped[str | None] = mapped_column(String(200))
    description_en: Mapped[str] = mapped_column(Text, default="")
    description_ko: Mapped[str | None] = mapped_column(Text)
    price_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    serving_description: Mapped[str | None] = mapped_column(String(200))
    spice_level: Mapped[int] = mapped_column(Integer, default=0)
    taste_profile: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    local_tips: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    badge: Mapped[str | None] = mapped_column(String(40))
    image_url: Mapped[str | None] = mapped_column(Text)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[MenuCategory] = relationship(back_populates="items")
    translations: Mapped[list[MenuItemTranslation]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan"
    )
    ingredient_links: Mapped[list[MenuItemIngredient]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan"
    )
    allergen_links: Mapped[list[MenuItemAllergen]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan"
    )
    dietary_claims: Mapped[list[MenuItemDietaryClaim]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan"
    )


class MenuItemTranslation(Base):
    __tablename__ = "menu_item_translations"

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(String(35), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    pronunciation: Mapped[str | None] = mapped_column(String(200))

    menu_item: Mapped[MenuItem] = relationship(back_populates="translations")


class Ingredient(Base):
    __tablename__ = "ingredients"

    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(100))
    name_ko: Mapped[str | None] = mapped_column(String(100))
    emoji: Mapped[str | None] = mapped_column(String(20))


class Allergen(Base):
    __tablename__ = "allergens"

    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(100))
    name_ko: Mapped[str | None] = mapped_column(String(100))


class MenuItemIngredient(Base):
    __tablename__ = "menu_item_ingredients"

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    ingredient_code: Mapped[str] = mapped_column(
        ForeignKey("ingredients.code", ondelete="RESTRICT"), primary_key=True
    )
    detail_en: Mapped[str | None] = mapped_column(String(300))
    detail_ko: Mapped[str | None] = mapped_column(String(300))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    menu_item: Mapped[MenuItem] = relationship(back_populates="ingredient_links")
    ingredient: Mapped[Ingredient] = relationship()


class MenuItemAllergen(Base):
    __tablename__ = "menu_item_allergens"

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    allergen_code: Mapped[str] = mapped_column(
        ForeignKey("allergens.code", ondelete="RESTRICT"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(30), primary_key=True, default="contains")
    verification_status: Mapped[str] = mapped_column(String(30), default="merchant_reported")
    source: Mapped[str | None] = mapped_column(String(100))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    menu_item: Mapped[MenuItem] = relationship(back_populates="allergen_links")
    allergen: Mapped[Allergen] = relationship()


class MenuItemDietaryClaim(Base):
    __tablename__ = "menu_item_dietary_claims"

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    verification_status: Mapped[str] = mapped_column(String(30), default="merchant_reported")

    menu_item: Mapped[MenuItem] = relationship(back_populates="dietary_claims")


class SavedRestaurant(Base):
    __tablename__ = "saved_restaurants"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RestaurantMembership(Base):
    __tablename__ = "restaurant_memberships"
    __table_args__ = (
        Index(
            "uq_restaurant_memberships_active_owner",
            "restaurant_id",
            unique=True,
            sqlite_where=text("role = 'owner' AND status = 'active'"),
            postgresql_where=text("role = 'owner' AND status = 'active'"),
        ),
    )

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(30), default="manager")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_reviews_item_created", "menu_item_id", "created_at"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), index=True
    )
    author_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    author_display_name: Mapped[str] = mapped_column(String(100))
    author_country_code: Mapped[str | None] = mapped_column(String(2))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)


class ExploreVideo(TimestampMixin, Base):
    __tablename__ = "explore_videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(30), default="youtube")
    provider_video_id: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    creator: Mapped[str] = mapped_column(String(160))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="prepared")
    serving_mode: Mapped[str] = mapped_column(String(30), default="dine_in")
    subtotal_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="KRW")
    korean_phrase: Mapped[str] = mapped_column(Text)
    translated_phrase: Mapped[str] = mapped_column(Text)
    allergy_note_ko: Mapped[str | None] = mapped_column(Text)
    allergy_note_localized: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("menu_items.id", ondelete="SET NULL")
    )
    name_en_snapshot: Mapped[str] = mapped_column(String(160))
    name_ko_snapshot: Mapped[str] = mapped_column(String(160))
    unit_price_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(500))

    order: Mapped[Order] = relationship(back_populates="items")


class Cart(TimestampMixin, Base):
    __tablename__ = "carts"
    __table_args__ = (Index("ix_carts_user_status", "user_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    serving_mode: Mapped[str] = mapped_column(String(30), default="dine_in")
    table_label: Mapped[str | None] = mapped_column(String(50))
    menu_revision: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "menu_item_id"),
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity_positive"),
        CheckConstraint("unit_price_snapshot >= 0", name="ck_cart_item_price_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500))
    request_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    cart: Mapped[Cart] = relationship(back_populates="items")


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30), default="direct")
    restaurant_id: Mapped[str | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(160))
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    participants: Mapped[list[ConversationParticipant]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="participants")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        UniqueConstraint(
            "conversation_id",
            "sender_user_id",
            "client_message_id",
            name="uq_message_client_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    sender_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    client_message_id: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20), default="text")
    media_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class MediaAsset(TimestampMixin, Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="uploaded")


class OwnerApplication(TimestampMixin, Base):
    __tablename__ = "owner_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    applicant_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    restaurant_id: Mapped[str | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL"), index=True
    )
    business_name: Mapped[str] = mapped_column(String(200))
    registration_number: Mapped[str] = mapped_column(String(30), index=True)
    address: Mapped[str] = mapped_column(String(300))
    phone: Mapped[str] = mapped_column(String(30))
    license_media_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT")
    )
    agreed_to_terms_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    terms_version: Mapped[str] = mapped_column(String(30))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QRCode(TimestampMixin, Base):
    __tablename__ = "qr_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Only a SHA-256 digest is stored; the printable secret is returned once on creation.
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    public_hint: Mapped[str] = mapped_column(String(12))
    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str | None] = mapped_column(String(100))
    table_label: Mapped[str | None] = mapped_column(String(50))
    purpose: Mapped[str] = mapped_column(String(30), default="restaurant_entry")
    menu_revision: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QRScan(Base):
    __tablename__ = "qr_scans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    qr_code_id: Mapped[str] = mapped_column(
        ForeignKey("qr_codes.id", ondelete="CASCADE"), index=True
    )
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locale: Mapped[str | None] = mapped_column(String(35))
    client_type: Mapped[str] = mapped_column(String(20), default="web")
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_resource", "resource_type", "resource_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
