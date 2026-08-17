from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal

SAFE_CATALOG_IMPORT_ENVIRONMENTS = {"local", "test"}
_IMPORT_NAMESPACE = uuid.UUID("28bd098b-05cc-4c85-b5c5-e557817f8420")
_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_CODE_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_LOCALE_PATTERN = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*$"
_CURRENCY_PATTERN = r"^[A-Z]{3}$"
_MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class CatalogImportError(RuntimeError):
    """Raised when a valid manifest cannot safely be applied to the target DB."""


class ManifestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class IngredientSpec(ManifestModel):
    code: str = Field(min_length=1, max_length=60, pattern=_CODE_PATTERN)
    name_en: str = Field(min_length=1, max_length=100)
    name_ko: str | None = Field(default=None, min_length=1, max_length=100)
    emoji: str | None = Field(default=None, min_length=1, max_length=20)


class AllergenSpec(ManifestModel):
    code: str = Field(min_length=1, max_length=60, pattern=_CODE_PATTERN)
    name_en: str = Field(min_length=1, max_length=100)
    name_ko: str | None = Field(default=None, min_length=1, max_length=100)


class RestaurantTranslationSpec(ManifestModel):
    locale: str = Field(min_length=2, max_length=35, pattern=_LOCALE_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10_000)
    address: str = Field(min_length=1, max_length=300)


class CategoryTranslationSpec(ManifestModel):
    locale: str = Field(min_length=2, max_length=35, pattern=_LOCALE_PATTERN)
    name: str = Field(min_length=1, max_length=100)


class ItemTranslationSpec(ManifestModel):
    locale: str = Field(min_length=2, max_length=35, pattern=_LOCALE_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10_000)
    pronunciation: str | None = Field(default=None, max_length=200)


class OpeningHourSpec(ManifestModel):
    day_of_week: int = Field(ge=0, le=6)
    opens_at: time | None = None
    closes_at: time | None = None
    is_closed: bool = False

    @model_validator(mode="after")
    def validate_times(self) -> OpeningHourSpec:
        if self.is_closed:
            if self.opens_at is not None or self.closes_at is not None:
                raise ValueError("closed days must omit opens_at and closes_at")
        elif self.opens_at is None or self.closes_at is None:
            raise ValueError("open days require both opens_at and closes_at")
        return self


class MediaSpec(ManifestModel):
    kind: str = Field(default="image", min_length=1, max_length=30)
    url: str = Field(min_length=1, max_length=2_000)
    thumbnail_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    duration_seconds: int | None = Field(default=None, ge=0)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    provider_video_id: str | None = Field(default=None, min_length=1, max_length=100)
    alt_text: str | None = Field(default=None, max_length=500)


class LocalTipSpec(ManifestModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=500)


class IngredientLinkSpec(ManifestModel):
    code: str = Field(min_length=1, max_length=60, pattern=_CODE_PATTERN)
    detail_en: str | None = Field(default=None, max_length=300)
    detail_ko: str | None = Field(default=None, max_length=300)
    is_primary: bool = False
    sort_order: int = Field(default=0, ge=0)


class AllergenLinkSpec(ManifestModel):
    code: str = Field(min_length=1, max_length=60, pattern=_CODE_PATTERN)
    relation_type: Literal[
        "contains", "may_contain", "cross_contact", "free_from", "does_not_contain"
    ]
    verification_status: Literal[
        "merchant_reported", "verified", "unverified", "unknown"
    ] = "merchant_reported"
    source: str | None = Field(default=None, max_length=100)
    verified_at: datetime | None = None


class DietaryClaimSpec(ManifestModel):
    code: str = Field(min_length=1, max_length=60, pattern=_CODE_PATTERN)
    verification_status: Literal[
        "merchant_reported", "verified", "unverified", "unknown"
    ] = "merchant_reported"


class MenuItemSpec(ManifestModel):
    slug: str = Field(min_length=1, max_length=120, pattern=_SLUG_PATTERN)
    name_en: str = Field(min_length=1, max_length=160)
    name_ko: str | None = Field(default=None, min_length=1, max_length=160)
    pronunciation: str | None = Field(default=None, max_length=200)
    description_en: str = Field(default="", max_length=10_000)
    description_ko: str | None = Field(default=None, max_length=10_000)
    price_amount: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=_CURRENCY_PATTERN)
    serving_description: str | None = Field(default=None, max_length=200)
    spice_level: int = Field(default=0, ge=0, le=5)
    taste_profile: dict[str, float] = Field(default_factory=dict)
    local_tips: list[LocalTipSpec] = Field(default_factory=list)
    badge: str | None = Field(default=None, max_length=40)
    image_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    media: list[MediaSpec] = Field(default_factory=list)
    is_available: bool = True
    sort_order: int = Field(default=0, ge=0)
    translations: list[ItemTranslationSpec] = Field(default_factory=list)
    ingredients: list[IngredientLinkSpec] = Field(default_factory=list)
    allergens: list[AllergenLinkSpec] = Field(default_factory=list)
    dietary_claims: list[DietaryClaimSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_nested_keys(self) -> MenuItemSpec:
        _ensure_unique(self.translations, "locale", f"item {self.slug} translation locale")
        _ensure_unique(self.ingredients, "code", f"item {self.slug} ingredient code")
        _ensure_unique(self.allergens, "code", f"item {self.slug} allergen code")
        _ensure_unique(self.dietary_claims, "code", f"item {self.slug} dietary claim")
        for taste, strength in self.taste_profile.items():
            if not taste.strip():
                raise ValueError(f"item {self.slug} has a blank taste_profile key")
            if strength < 0 or strength > 1:
                raise ValueError(
                    f"item {self.slug} taste_profile values must be between 0 and 1"
                )
        return self


class MenuCategorySpec(ManifestModel):
    slug: str = Field(min_length=1, max_length=100, pattern=_SLUG_PATTERN)
    name_en: str = Field(min_length=1, max_length=100)
    name_ko: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True
    translations: list[CategoryTranslationSpec] = Field(default_factory=list)
    items: list[MenuItemSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_nested_keys(self) -> MenuCategorySpec:
        _ensure_unique(self.translations, "locale", f"category {self.slug} translation locale")
        _ensure_unique(self.items, "slug", f"category {self.slug} item slug")
        return self


class RestaurantSpec(ManifestModel):
    slug: str = Field(min_length=1, max_length=99, pattern=_SLUG_PATTERN)
    name_en: str = Field(min_length=1, max_length=160)
    name_ko: str | None = Field(default=None, min_length=1, max_length=160)
    description_en: str = Field(default="", max_length=10_000)
    description_ko: str | None = Field(default=None, max_length=10_000)
    handle: str | None = Field(default=None, min_length=2, max_length=100)
    category: str = Field(min_length=1, max_length=80)
    hero_style: str = Field(default="charcoal", min_length=1, max_length=30)
    address_en: str = Field(min_length=1, max_length=300)
    address_ko: str | None = Field(default=None, min_length=1, max_length=300)
    phone: str | None = Field(default=None, max_length=30)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    currency: str = Field(default="KRW", pattern=_CURRENCY_PATTERN)
    timezone_name: str = Field(default="Asia/Seoul", min_length=1, max_length=50)
    is_verified: bool = False
    is_open: bool = True
    is_published: bool = False
    cover_image_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    gallery: list[MediaSpec] = Field(default_factory=list)
    translations: list[RestaurantTranslationSpec] = Field(default_factory=list)
    hours: list[OpeningHourSpec] = Field(default_factory=list)
    menu_categories: list[MenuCategorySpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_nested_keys(self) -> RestaurantSpec:
        if self.handle is not None and not self.handle.startswith("@"):
            raise ValueError(f"restaurant {self.slug} handle must start with @")
        _ensure_unique(self.translations, "locale", f"restaurant {self.slug} translation locale")
        _ensure_unique(self.hours, "day_of_week", f"restaurant {self.slug} opening day")
        _ensure_unique(self.menu_categories, "slug", f"restaurant {self.slug} category slug")
        item_slugs = [item.slug for category in self.menu_categories for item in category.items]
        if len(item_slugs) != len(set(item_slugs)):
            raise ValueError(f"restaurant {self.slug} contains duplicate menu item slugs")
        return self


class CatalogManifest(ManifestModel):
    schema_version: Literal[1]
    ingredients: list[IngredientSpec] = Field(default_factory=list)
    allergens: list[AllergenSpec] = Field(default_factory=list)
    restaurants: list[RestaurantSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_stable_keys(self) -> CatalogManifest:
        _ensure_unique(self.ingredients, "code", "ingredient code")
        _ensure_unique(self.allergens, "code", "allergen code")
        _ensure_unique(self.restaurants, "slug", "restaurant slug")
        handles = [restaurant.handle or f"@{restaurant.slug}" for restaurant in self.restaurants]
        if len(handles) != len(set(handles)):
            raise ValueError("manifest contains duplicate restaurant handles")
        return self


def _ensure_unique(values: list[Any], key: str, label: str) -> None:
    keys = [getattr(value, key) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"manifest contains duplicate {label} values")


@dataclass
class ChangeCount:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass
class CatalogImportSummary:
    resources: dict[str, ChangeCount] = field(default_factory=dict)

    def record(self, resource: str, outcome: Literal["inserted", "updated", "unchanged"]) -> None:
        counter = self.resources.setdefault(resource, ChangeCount())
        setattr(counter, outcome, getattr(counter, outcome) + 1)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {name: asdict(counts) for name, counts in sorted(self.resources.items())}


def validate_catalog_environment(environment: str) -> None:
    if environment not in SAFE_CATALOG_IMPORT_ENVIRONMENTS:
        raise CatalogImportError(
            "Catalog import is restricted to local and test environments; "
            "staging/production imports require a reviewed deployment process."
        )


def load_catalog_manifest(path: Path) -> CatalogManifest:
    """Read and fully validate the JSON document before a DB session is opened."""

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise CatalogImportError(f"Could not read catalog manifest: {path}") from exc
    if len(raw_bytes) > _MAX_MANIFEST_BYTES:
        raise CatalogImportError("Catalog manifest exceeds the 10 MiB safety limit")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogImportError("Catalog manifest must be UTF-8 JSON") from exc
    try:
        return CatalogManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise CatalogImportError(f"Invalid catalog manifest:\n{exc}") from exc


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(_IMPORT_NAMESPACE, ":".join((kind, *parts))))


def _assign(row: Any, values: dict[str, Any]) -> bool:
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


def _fields_for_update(spec: ManifestModel, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(spec, name) for name in names if name in spec.model_fields_set}


def _preflight_database_references(db: Session, manifest: CatalogManifest) -> None:
    declared_ingredients = {value.code for value in manifest.ingredients}
    declared_allergens = {value.code for value in manifest.allergens}
    referenced_ingredients = {
        link.code
        for restaurant in manifest.restaurants
        for category in restaurant.menu_categories
        for item in category.items
        for link in item.ingredients
    }
    referenced_allergens = {
        link.code
        for restaurant in manifest.restaurants
        for category in restaurant.menu_categories
        for item in category.items
        for link in item.allergens
    }
    existing_ingredients = set(
        db.scalars(
            select(models.Ingredient.code).where(
                models.Ingredient.code.in_(referenced_ingredients - declared_ingredients)
            )
        )
    )
    existing_allergens = set(
        db.scalars(
            select(models.Allergen.code).where(
                models.Allergen.code.in_(referenced_allergens - declared_allergens)
            )
        )
    )
    missing_ingredients = referenced_ingredients - declared_ingredients - existing_ingredients
    missing_allergens = referenced_allergens - declared_allergens - existing_allergens
    if missing_ingredients or missing_allergens:
        details = []
        if missing_ingredients:
            details.append(f"unknown ingredient codes: {', '.join(sorted(missing_ingredients))}")
        if missing_allergens:
            details.append(f"unknown allergen codes: {', '.join(sorted(missing_allergens))}")
        raise CatalogImportError("; ".join(details))

    for restaurant in manifest.restaurants:
        existing = db.scalar(
            select(models.Restaurant).where(models.Restaurant.slug == restaurant.slug)
        )
        desired_handle = restaurant.handle
        if existing is None and desired_handle is None:
            desired_handle = f"@{restaurant.slug}"
        if desired_handle is None:
            continue
        collision = db.scalar(
            select(models.Restaurant).where(models.Restaurant.handle == desired_handle)
        )
        if collision is not None and collision.slug != restaurant.slug:
            raise CatalogImportError(
                f"restaurant handle {desired_handle!r} already belongs to {collision.slug!r}"
            )


def _upsert_taxonomy(
    db: Session, manifest: CatalogManifest, summary: CatalogImportSummary
) -> None:
    for spec in manifest.ingredients:
        row = db.get(models.Ingredient, spec.code)
        values = spec.model_dump()
        if row is None:
            db.add(models.Ingredient(**values))
            summary.record("ingredients", "inserted")
        elif _assign(row, _fields_for_update(spec, tuple(values))):
            summary.record("ingredients", "updated")
        else:
            summary.record("ingredients", "unchanged")

    for spec in manifest.allergens:
        row = db.get(models.Allergen, spec.code)
        values = spec.model_dump()
        if row is None:
            db.add(models.Allergen(**values))
            summary.record("allergens", "inserted")
        elif _assign(row, _fields_for_update(spec, tuple(values))):
            summary.record("allergens", "updated")
        else:
            summary.record("allergens", "unchanged")
    db.flush()


_RESTAURANT_FIELDS = (
    "name_en",
    "name_ko",
    "description_en",
    "description_ko",
    "handle",
    "category",
    "hero_style",
    "address_en",
    "address_ko",
    "phone",
    "latitude",
    "longitude",
    "currency",
    "timezone_name",
    "is_verified",
    "is_open",
    "is_published",
    "cover_image_url",
    "gallery",
)


def _restaurant_values(spec: RestaurantSpec, *, new: bool) -> dict[str, Any]:
    fields = set(_RESTAURANT_FIELDS) if new else spec.model_fields_set
    values: dict[str, Any] = {}
    for name in _RESTAURANT_FIELDS:
        if name not in fields:
            continue
        value = getattr(spec, name)
        if name == "gallery":
            value = [media.model_dump(exclude_none=True) for media in value]
        values[name] = value
    if new and values["handle"] is None:
        values["handle"] = f"@{spec.slug}"
    return values


def _upsert_restaurant_translation(
    db: Session,
    restaurant_id: str,
    spec: RestaurantTranslationSpec,
    summary: CatalogImportSummary,
) -> None:
    identity = (restaurant_id, spec.locale)
    row = db.get(models.RestaurantTranslation, identity)
    values = {
        "name": spec.name,
        "description": spec.description,
        "address": spec.address,
    }
    if row is None:
        db.add(
            models.RestaurantTranslation(
                restaurant_id=restaurant_id,
                locale=spec.locale,
                **values,
            )
        )
        summary.record("restaurant_translations", "inserted")
    elif _assign(row, _fields_for_update(spec, tuple(values))):
        summary.record("restaurant_translations", "updated")
    else:
        summary.record("restaurant_translations", "unchanged")


def _upsert_hours(
    db: Session,
    restaurant_slug: str,
    restaurant_id: str,
    spec: OpeningHourSpec,
    summary: CatalogImportSummary,
) -> None:
    row = db.scalar(
        select(models.OpeningHour).where(
            models.OpeningHour.restaurant_id == restaurant_id,
            models.OpeningHour.day_of_week == spec.day_of_week,
        )
    )
    values = {
        "opens_at": spec.opens_at,
        "closes_at": spec.closes_at,
        "is_closed": spec.is_closed,
    }
    if row is None:
        db.add(
            models.OpeningHour(
                id=_stable_id("opening-hour", restaurant_slug, str(spec.day_of_week)),
                restaurant_id=restaurant_id,
                day_of_week=spec.day_of_week,
                **values,
            )
        )
        summary.record("opening_hours", "inserted")
    elif _assign(row, _fields_for_update(spec, tuple(values))):
        summary.record("opening_hours", "updated")
    else:
        summary.record("opening_hours", "unchanged")


_CATEGORY_FIELDS = ("name_en", "name_ko", "sort_order", "is_active")


def _upsert_category(
    db: Session,
    restaurant_slug: str,
    restaurant_id: str,
    spec: MenuCategorySpec,
    summary: CatalogImportSummary,
) -> tuple[models.MenuCategory, bool]:
    row = db.scalar(
        select(models.MenuCategory).where(
            models.MenuCategory.restaurant_id == restaurant_id,
            models.MenuCategory.slug == spec.slug,
        )
    )
    if row is None:
        row = models.MenuCategory(
            id=_stable_id("menu-category", restaurant_slug, spec.slug),
            restaurant_id=restaurant_id,
            slug=spec.slug,
            **{name: getattr(spec, name) for name in _CATEGORY_FIELDS},
        )
        db.add(row)
        summary.record("menu_categories", "inserted")
        changed = True
    else:
        changed = _assign(row, _fields_for_update(spec, _CATEGORY_FIELDS))
        summary.record("menu_categories", "updated" if changed else "unchanged")
    db.flush()

    for translation in spec.translations:
        identity = (row.id, translation.locale)
        translated = db.get(models.MenuCategoryTranslation, identity)
        values = {"name": translation.name}
        if translated is None:
            db.add(
                models.MenuCategoryTranslation(
                    category_id=row.id,
                    locale=translation.locale,
                    **values,
                )
            )
            summary.record("category_translations", "inserted")
            changed = True
        elif _assign(translated, _fields_for_update(translation, tuple(values))):
            summary.record("category_translations", "updated")
            changed = True
        else:
            summary.record("category_translations", "unchanged")
    return row, changed


_ITEM_FIELDS = (
    "name_en",
    "name_ko",
    "pronunciation",
    "description_en",
    "description_ko",
    "price_amount",
    "currency",
    "serving_description",
    "spice_level",
    "taste_profile",
    "local_tips",
    "badge",
    "image_url",
    "media",
    "is_available",
    "sort_order",
)


def _item_values(
    spec: MenuItemSpec, *, new: bool, restaurant_currency: str
) -> dict[str, Any]:
    fields = set(_ITEM_FIELDS) if new else spec.model_fields_set
    values: dict[str, Any] = {}
    for name in _ITEM_FIELDS:
        if name not in fields:
            continue
        value = getattr(spec, name)
        if name == "currency" and value is None:
            if not new:
                continue
            value = restaurant_currency
        elif name == "local_tips":
            value = [tip.model_dump() for tip in value]
        elif name == "media":
            value = [media.model_dump(exclude_none=True) for media in value]
        values[name] = value
    return values


def _upsert_item_translation(
    db: Session,
    item_id: str,
    spec: ItemTranslationSpec,
    summary: CatalogImportSummary,
) -> bool:
    identity = (item_id, spec.locale)
    row = db.get(models.MenuItemTranslation, identity)
    values = {
        "name": spec.name,
        "description": spec.description,
        "pronunciation": spec.pronunciation,
    }
    if row is None:
        db.add(models.MenuItemTranslation(menu_item_id=item_id, locale=spec.locale, **values))
        summary.record("item_translations", "inserted")
        return True
    changed = _assign(row, _fields_for_update(spec, tuple(values)))
    summary.record("item_translations", "updated" if changed else "unchanged")
    return changed


def _upsert_ingredient_link(
    db: Session,
    item_id: str,
    spec: IngredientLinkSpec,
    summary: CatalogImportSummary,
) -> bool:
    identity = (item_id, spec.code)
    row = db.get(models.MenuItemIngredient, identity)
    values = {
        "detail_en": spec.detail_en,
        "detail_ko": spec.detail_ko,
        "is_primary": spec.is_primary,
        "sort_order": spec.sort_order,
    }
    if row is None:
        db.add(
            models.MenuItemIngredient(
                menu_item_id=item_id,
                ingredient_code=spec.code,
                **values,
            )
        )
        summary.record("item_ingredients", "inserted")
        return True
    changed = _assign(row, _fields_for_update(spec, tuple(values)))
    summary.record("item_ingredients", "updated" if changed else "unchanged")
    return changed


def _upsert_allergen_link(
    db: Session,
    item_id: str,
    spec: AllergenLinkSpec,
    summary: CatalogImportSummary,
) -> bool:
    rows = list(
        db.scalars(
            select(models.MenuItemAllergen).where(
                models.MenuItemAllergen.menu_item_id == item_id,
                models.MenuItemAllergen.allergen_code == spec.code,
            )
        )
    )
    if len(rows) > 1:
        raise CatalogImportError(
            f"menu item {item_id} has ambiguous existing allergen rows for {spec.code!r}"
        )
    values = {
        "relation_type": spec.relation_type,
        "verification_status": spec.verification_status,
        "source": spec.source,
        "verified_at": spec.verified_at,
    }
    if not rows:
        db.add(
            models.MenuItemAllergen(
                menu_item_id=item_id,
                allergen_code=spec.code,
                **values,
            )
        )
        summary.record("item_allergens", "inserted")
        return True
    changed = _assign(rows[0], _fields_for_update(spec, tuple(values)))
    summary.record("item_allergens", "updated" if changed else "unchanged")
    return changed


def _upsert_dietary_claim(
    db: Session,
    item_id: str,
    spec: DietaryClaimSpec,
    summary: CatalogImportSummary,
) -> bool:
    identity = (item_id, spec.code)
    row = db.get(models.MenuItemDietaryClaim, identity)
    values = {"verification_status": spec.verification_status}
    if row is None:
        db.add(
            models.MenuItemDietaryClaim(
                menu_item_id=item_id,
                code=spec.code,
                **values,
            )
        )
        summary.record("item_dietary_claims", "inserted")
        return True
    changed = _assign(row, _fields_for_update(spec, tuple(values)))
    summary.record("item_dietary_claims", "updated" if changed else "unchanged")
    return changed


def _upsert_item(
    db: Session,
    restaurant: models.Restaurant,
    category: models.MenuCategory,
    spec: MenuItemSpec,
    summary: CatalogImportSummary,
) -> bool:
    row = db.scalar(
        select(models.MenuItem).where(
            models.MenuItem.restaurant_id == restaurant.id,
            models.MenuItem.slug == spec.slug,
        )
    )
    if row is None:
        row = models.MenuItem(
            id=_stable_id("menu-item", restaurant.slug, spec.slug),
            restaurant_id=restaurant.id,
            category_id=category.id,
            slug=spec.slug,
            **_item_values(spec, new=True, restaurant_currency=restaurant.currency),
        )
        db.add(row)
        summary.record("menu_items", "inserted")
        changed = True
    else:
        values = _item_values(spec, new=False, restaurant_currency=restaurant.currency)
        values["category_id"] = category.id
        changed = _assign(row, values)
        summary.record("menu_items", "updated" if changed else "unchanged")
    db.flush()

    for translation in spec.translations:
        changed |= _upsert_item_translation(db, row.id, translation, summary)
    for ingredient in spec.ingredients:
        changed |= _upsert_ingredient_link(db, row.id, ingredient, summary)
    for allergen in spec.allergens:
        changed |= _upsert_allergen_link(db, row.id, allergen, summary)
    for claim in spec.dietary_claims:
        changed |= _upsert_dietary_claim(db, row.id, claim, summary)
    return changed


def _upsert_restaurants(
    db: Session, manifest: CatalogManifest, summary: CatalogImportSummary
) -> None:
    for spec in manifest.restaurants:
        row = db.scalar(select(models.Restaurant).where(models.Restaurant.slug == spec.slug))
        is_new = row is None
        if row is None:
            row = models.Restaurant(
                id=_stable_id("restaurant", spec.slug),
                slug=spec.slug,
                owner_user_id=None,
                rating_avg=Decimal("0.0"),
                rating_count=0,
                menu_revision=1,
                **_restaurant_values(spec, new=True),
            )
            db.add(row)
            summary.record("restaurants", "inserted")
        else:
            changed = _assign(row, _restaurant_values(spec, new=False))
            summary.record("restaurants", "updated" if changed else "unchanged")
        db.flush()

        for translation in spec.translations:
            _upsert_restaurant_translation(db, row.id, translation, summary)
        for opening_hour in spec.hours:
            _upsert_hours(db, spec.slug, row.id, opening_hour, summary)

        menu_changed = False
        for category_spec in spec.menu_categories:
            category, category_changed = _upsert_category(
                db, spec.slug, row.id, category_spec, summary
            )
            menu_changed |= category_changed
            for item_spec in category_spec.items:
                menu_changed |= _upsert_item(db, row, category, item_spec, summary)
        if menu_changed and not is_new:
            row.menu_revision += 1
    db.flush()


def import_catalog(
    db: Session,
    manifest: CatalogManifest,
    *,
    environment: str,
    apply: bool = False,
) -> CatalogImportSummary:
    """Atomically upsert a fully validated manifest; dry-run and rollback by default."""

    validate_catalog_environment(environment)
    summary = CatalogImportSummary()
    try:
        _preflight_database_references(db, manifest)
        _upsert_taxonomy(db, manifest, summary)
        _upsert_restaurants(db, manifest, summary)
        db.flush()
        if apply:
            db.commit()
        else:
            db.rollback()
        return summary
    except Exception:
        db.rollback()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and upsert a Fofu restaurant/menu JSON manifest. "
            "The default is a transactionally checked dry-run."
        )
    )
    parser.add_argument("manifest", type=Path, help="Path to a schema_version=1 JSON manifest")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the import (without this flag every change is rolled back)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    try:
        validate_catalog_environment(settings.environment)
        manifest = load_catalog_manifest(args.manifest)
        with SessionLocal() as db:
            summary = import_catalog(
                db,
                manifest,
                environment=settings.environment,
                apply=args.apply,
            )
    except CatalogImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = "APPLIED" if args.apply else "DRY RUN (rolled back)"
    safe_database_url = make_url(settings.database_url).render_as_string(hide_password=True)
    print(f"{mode}: {args.manifest} -> {safe_database_url}")
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
