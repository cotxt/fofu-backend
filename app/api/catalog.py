from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.dependencies import OPTIONAL_AUTH_OPENAPI, DBSession, OptionalUser
from app.schemas.catalog import (
    ExplorePage,
    MenuItemDetail,
    RestaurantDetail,
    RestaurantMenu,
    RestaurantPage,
    ReviewPage,
    SearchFacets,
    SearchResults,
    TrendingSearches,
)
from app.services import catalog as service
from app.utils import normalize_locale

router = APIRouter(tags=["catalog"])


def request_locale(
    locale: Annotated[str | None, Query(max_length=35)] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    raw = locale
    if not raw and accept_language:
        raw = accept_language.split(",", 1)[0].split(";", 1)[0].strip()
    return normalize_locale(raw)


CatalogLocale = Annotated[str, Depends(request_locale)]


def _split_codes(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _discovery_filters(
    filters: str | None,
    diets: list[str],
    excluded_allergens: list[str],
    ingredients: list[str],
    spicy: bool | None,
) -> tuple[list[str], list[str], list[str], bool | None]:
    for raw_filter in _split_codes(filters):
        code = raw_filter.casefold().replace("_", "-").replace(" ", "-")
        if code in {"all", ""}:
            continue
        if code in {"no-pork", "pork-free"}:
            excluded_allergens.append("pork")
        elif code in {"veggie", "vegetarian"}:
            diets.append("vegetarian")
        elif code in {"nut-free", "no-nuts"}:
            excluded_allergens.extend(("peanut", "tree-nut"))
        elif code == "halal":
            diets.append("halal")
        elif code == "spicy":
            spicy = True
        elif code == "seafood":
            diets.append("pescatarian")
        else:
            ingredients.append(code)
    return diets, excluded_allergens, ingredients, spicy


@router.get(
    "/restaurants",
    response_model=RestaurantPage,
    response_model_exclude_none=True,
    summary="List nearby restaurants",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def restaurants(
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
    lat: Annotated[float, Query(ge=-90, le=90)] = 37.5563,
    lng: Annotated[float, Query(ge=-180, le=180)] = 126.9236,
    radius_m: Annotated[int, Query(ge=100, le=50_000)] = 5_000,
    filters: Annotated[str | None, Query(description="Comma-separated discovery filters")] = None,
    diet: Annotated[str | None, Query(description="Comma-separated dietary claim codes")] = None,
    exclude_allergen: Annotated[
        str | None, Query(description="Comma-separated allergen codes to exclude")
    ] = None,
    ingredient: Annotated[str | None, Query(description="Required ingredient codes")] = None,
    main_ingredient: Annotated[
        str | None,
        Query(description="Comma-separated main-ingredient facets (OR within the facet)"),
    ] = None,
    dish_type: Annotated[
        str | None, Query(description="Comma-separated dish-type facets (OR within the facet)")
    ] = None,
    price: Annotated[
        str | None, Query(description="Comma-separated price-bucket facets (OR within the facet)")
    ] = None,
    taste: Annotated[
        str | None, Query(description="Comma-separated taste facets (OR within the facet)")
    ] = None,
    spicy: bool | None = None,
    solo_friendly: bool = False,
    open_now: bool = False,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    category: Annotated[str | None, Query(max_length=80)] = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> RestaurantPage:
    diets, excluded, ingredients, spicy = _discovery_filters(
        filters,
        _split_codes(diet),
        _split_codes(exclude_allergen),
        _split_codes(ingredient),
        spicy,
    )
    return service.nearby_restaurants(
        db,
        latitude=lat,
        longitude=lng,
        radius_m=radius_m,
        locale=locale,
        diet_codes=diets,
        excluded_allergen_codes=excluded,
        ingredient_codes=ingredients,
        main_ingredient_codes=_split_codes(main_ingredient),
        dish_type_codes=_split_codes(dish_type),
        price_codes=_split_codes(price),
        taste_codes=_split_codes(taste),
        spicy=spicy,
        solo_friendly=solo_friendly,
        open_now=open_now,
        min_rating=min_rating,
        category=category,
        cursor=cursor,
        limit=limit,
        user=user,
    )


@router.get(
    "/restaurants/nearby",
    response_model=RestaurantPage,
    response_model_exclude_none=True,
    include_in_schema=False,
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def restaurants_nearby_alias(
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    radius_m: Annotated[int, Query(ge=100, le=50_000)] = 5_000,
    filters: str | None = None,
    diet: str | None = None,
    exclude_allergen: str | None = None,
    ingredient: str | None = None,
    main_ingredient: str | None = None,
    dish_type: str | None = None,
    price: str | None = None,
    taste: str | None = None,
    spicy: bool | None = None,
    solo_friendly: bool = False,
    open_now: bool = False,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    category: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> RestaurantPage:
    diets, excluded, ingredients, spicy = _discovery_filters(
        filters,
        _split_codes(diet),
        _split_codes(exclude_allergen),
        _split_codes(ingredient),
        spicy,
    )
    return service.nearby_restaurants(
        db,
        latitude=lat,
        longitude=lng,
        radius_m=radius_m,
        locale=locale,
        diet_codes=diets,
        excluded_allergen_codes=excluded,
        ingredient_codes=ingredients,
        main_ingredient_codes=_split_codes(main_ingredient),
        dish_type_codes=_split_codes(dish_type),
        price_codes=_split_codes(price),
        taste_codes=_split_codes(taste),
        spicy=spicy,
        solo_friendly=solo_friendly,
        open_now=open_now,
        min_rating=min_rating,
        category=category,
        cursor=cursor,
        limit=limit,
        user=user,
    )


@router.get(
    "/search/facets",
    response_model=SearchFacets,
    response_model_exclude_none=True,
    summary="List DB-backed search facets",
)
def search_facet_options(db: DBSession, locale: CatalogLocale) -> SearchFacets:
    return service.search_facets(db, locale=locale)


@router.get(
    "/search/trending",
    response_model=TrendingSearches,
    response_model_exclude_none=True,
    summary="List curated trending searches backed by published menu items",
)
def search_trending(db: DBSession, locale: CatalogLocale) -> TrendingSearches:
    return service.trending_searches(db, locale=locale)


@router.get(
    "/search",
    response_model=SearchResults,
    response_model_exclude_none=True,
    summary="Search dishes, ingredients, and restaurants",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def search(
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
    q: Annotated[str, Query(max_length=120)] = "",
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius_m: Annotated[int | None, Query(ge=100, le=50_000)] = None,
    diet: str | None = None,
    exclude_allergen: str | None = None,
    ingredient: str | None = None,
    main_ingredient: Annotated[
        str | None,
        Query(description="Comma-separated main-ingredient facets (OR within the facet)"),
    ] = None,
    spicy: bool | None = None,
    max_price: Annotated[int | None, Query(ge=0)] = None,
    price: Annotated[
        str | None,
        Query(max_length=200, description="Comma-separated exact price buckets"),
    ] = None,
    taste: Annotated[
        str | None,
        Query(max_length=200, description="Comma-separated taste facets (OR within the facet)"),
    ] = None,
    dish_type: Annotated[
        str | None,
        Query(max_length=300, description="Comma-separated dish-type facets (OR within the facet)"),
    ] = None,
    solo_friendly: bool = False,
    open_now: bool = False,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResults:
    if (lat is None) != (lng is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "incomplete_coordinates",
                "message": "lat and lng must be supplied together.",
            },
        )
    if radius_m is not None and lat is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "coordinates_required",
                "message": "lat and lng are required when radius_m is supplied.",
            },
        )
    return service.search_catalog(
        db,
        query=q,
        locale=locale,
        latitude=lat,
        longitude=lng,
        radius_m=radius_m,
        diet_codes=_split_codes(diet),
        excluded_allergen_codes=_split_codes(exclude_allergen),
        ingredient_codes=_split_codes(ingredient),
        main_ingredient_codes=_split_codes(main_ingredient),
        dish_type_codes=_split_codes(dish_type),
        price_codes=_split_codes(price),
        taste_codes=_split_codes(taste),
        spicy=spicy,
        solo_friendly=solo_friendly,
        max_price=max_price,
        open_now=open_now,
        min_rating=min_rating,
        cursor=cursor,
        limit=limit,
        user=user,
    )


@router.get(
    "/explore",
    response_model=ExplorePage,
    response_model_exclude_none=True,
    summary="List the short-form food video feed",
)
def explore(
    db: DBSession,
    category: Annotated[str | None, Query(max_length=60)] = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
) -> ExplorePage:
    return service.explore_feed(db, category=category, cursor=cursor, limit=limit)


@router.get(
    "/restaurants/{restaurant_identifier}/menu",
    response_model=RestaurantMenu,
    response_model_exclude_none=True,
    summary="Get a restaurant menu",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def menu(
    restaurant_identifier: str,
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
) -> RestaurantMenu:
    return service.restaurant_menu(db, restaurant_identifier, locale=locale, user=user)


@router.get(
    "/restaurants/{restaurant_identifier}/menu-items/{item_identifier}",
    response_model=MenuItemDetail,
    response_model_exclude_none=True,
    summary="Get a menu item within a restaurant",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def nested_menu_item(
    restaurant_identifier: str,
    item_identifier: str,
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
) -> MenuItemDetail:
    return service.menu_item_detail(
        db,
        item_identifier,
        locale=locale,
        restaurant_identifier=restaurant_identifier,
        user=user,
    )


@router.get(
    "/restaurants/{restaurant_identifier}/menu-items/{item_identifier}/reviews",
    response_model=ReviewPage,
    response_model_exclude_none=True,
    include_in_schema=False,
)
def nested_menu_item_review_page(
    restaurant_identifier: str,
    item_identifier: str,
    db: DBSession,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ReviewPage:
    return service.menu_item_reviews(
        db,
        item_identifier,
        restaurant_identifier=restaurant_identifier,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/restaurants/{restaurant_identifier}",
    response_model=RestaurantDetail,
    response_model_exclude_none=True,
    summary="Get restaurant details",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def restaurant(
    restaurant_identifier: str,
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> RestaurantDetail:
    if (lat is None) != (lng is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "incomplete_coordinates",
                "message": "lat and lng must be supplied together.",
            },
        )
    return service.restaurant_detail(
        db,
        restaurant_identifier,
        locale=locale,
        latitude=lat,
        longitude=lng,
        user=user,
    )


@router.get(
    "/menu-items/{item_identifier}/reviews",
    response_model=ReviewPage,
    response_model_exclude_none=True,
    summary="List published reviews for a menu item",
)
def menu_item_review_page(
    item_identifier: str,
    db: DBSession,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> ReviewPage:
    return service.menu_item_reviews(db, item_identifier, cursor=cursor, limit=limit)


@router.get(
    "/menu-items/{item_identifier}",
    response_model=MenuItemDetail,
    response_model_exclude_none=True,
    summary="Get menu item details",
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def menu_item(
    item_identifier: str,
    db: DBSession,
    locale: CatalogLocale,
    user: OptionalUser,
) -> MenuItemDetail:
    return service.menu_item_detail(db, item_identifier, locale=locale, user=user)
