from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Money


class CatalogModel(BaseModel):
    """Strict platform-neutral contract shared by native and web clients."""

    model_config = ConfigDict(extra="forbid")


class Ingredient(CatalogModel):
    code: str
    name: str
    emoji: str | None = None
    detail: str | None = None
    is_primary: bool = False


class AllergenNotice(CatalogModel):
    code: str
    name: str
    relationship: str
    verification_status: str
    source: str | None = None


class DietaryClaim(CatalogModel):
    code: str
    verification_status: str


class Media(CatalogModel):
    kind: str
    url: str
    thumbnail_url: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    provider: str | None = None
    provider_video_id: str | None = None
    alt_text: str | None = None


class CompatibilityConflict(CatalogModel):
    code: str
    kind: Literal["allergen", "ingredient", "diet"]
    relation: str
    verification_status: str
    label: str


class MenuCompatibility(CatalogModel):
    status: Literal["compatible", "conflict", "unknown"]
    matched_conflicts: list[CompatibilityConflict] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    disclaimer: str


class OpeningHours(CatalogModel):
    day_of_week: int = Field(ge=0, le=6, description="Monday is 0 and Sunday is 6")
    opens_at: time | None
    closes_at: time | None
    is_closed: bool


class MenuItemSummary(CatalogModel):
    id: str
    restaurant_id: str
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
    media: list[Media] = Field(default_factory=list)
    is_available: bool
    is_orderable: bool
    orderability_reason: str | None = None
    ingredients: list[Ingredient] = Field(default_factory=list)
    allergens: list[AllergenNotice] = Field(default_factory=list)
    dietary_claims: list[DietaryClaim] = Field(default_factory=list)
    compatibility: MenuCompatibility


class MenuItemDetail(MenuItemSummary):
    taste_profile: dict[str, float] = Field(default_factory=dict)
    local_tips: list[dict[str, str]] = Field(default_factory=list)
    review_count: int = Field(default=0, ge=0)
    rating_avg: float | None = Field(default=None, ge=0, le=5)


class MenuCategory(CatalogModel):
    id: str
    slug: str
    name: str
    sort_order: int
    items: list[MenuItemSummary] = Field(default_factory=list)


class RestaurantSummary(CatalogModel):
    id: str
    slug: str
    name: str
    description: str
    handle: str
    category: str
    hero_style: str
    latitude: float
    longitude: float
    distance_m: int | None = Field(default=None, ge=0)
    rating_avg: float = Field(ge=0, le=5)
    rating_count: int = Field(ge=0)
    is_verified: bool
    is_open_now: bool
    cover_image_url: str | None = None
    featured_item: MenuItemSummary | None = None


class RestaurantDetail(RestaurantSummary):
    address: str
    original_address: str | None = None
    phone: str | None = None
    currency: str
    timezone: str
    gallery: list[dict[str, Any]] = Field(default_factory=list)
    hours: list[OpeningHours] = Field(default_factory=list)
    menu_category_count: int = Field(ge=0)
    menu_item_count: int = Field(ge=0)


class RestaurantMenu(CatalogModel):
    restaurant: RestaurantSummary
    categories: list[MenuCategory] = Field(default_factory=list)


class RestaurantPage(CatalogModel):
    items: list[RestaurantSummary] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    total: int = Field(ge=0)


class SearchResults(CatalogModel):
    query: str
    items: list[MenuItemSummary] = Field(default_factory=list)
    restaurants: list[RestaurantSummary] = Field(default_factory=list)
    item_count: int = Field(ge=0)
    restaurant_count: int = Field(ge=0)
    next_cursor: str | None = None
    has_more: bool


class SearchFacetOption(CatalogModel):
    code: str
    label: str
    count: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchFacetSection(CatalogModel):
    key: str
    title: str
    options: list[SearchFacetOption] = Field(default_factory=list)


class SearchFacets(CatalogModel):
    sections: list[SearchFacetSection] = Field(default_factory=list)


class TrendingSearch(CatalogModel):
    rank: int = Field(ge=1)
    query: str
    subtitle: str
    search_count: int = Field(ge=0)
    menu_item_id: str | None = None


class TrendingSearches(CatalogModel):
    items: list[TrendingSearch] = Field(default_factory=list)


class Review(CatalogModel):
    id: str
    menu_item_id: str
    rating: int = Field(ge=1, le=5)
    body: str
    author_display_name: str
    author_country_code: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class ReviewPage(CatalogModel):
    items: list[Review] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    total: int = Field(ge=0)


class ExploreVideo(CatalogModel):
    id: str
    provider: str
    provider_video_id: str
    title: str
    creator: str
    thumbnail_url: str | None = None
    playback_url: str
    categories: list[str] = Field(default_factory=list)


class ExplorePage(CatalogModel):
    items: list[ExploreVideo] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    total: int = Field(ge=0)
