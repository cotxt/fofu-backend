from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import Integer, Text, and_, case, cast, exists, func, literal, or_, select, text
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ExploreVideo as ExploreVideoModel,
)
from app.models import (
    Ingredient as IngredientModel,
)
from app.models import (
    MenuCategory as MenuCategoryModel,
)
from app.models import (
    MenuCategoryTranslation as MenuCategoryTranslationModel,
)
from app.models import (
    MenuItem as MenuItemModel,
)
from app.models import (
    MenuItemAllergen,
    MenuItemDietaryClaim,
    MenuItemIngredient,
)
from app.models import (
    MenuItemTranslation as MenuItemTranslationModel,
)
from app.models import (
    OpeningHour as OpeningHourModel,
)
from app.models import (
    Restaurant as RestaurantModel,
)
from app.models import (
    RestaurantTranslation as RestaurantTranslationModel,
)
from app.models import (
    Review as ReviewModel,
)
from app.models import (
    User as UserModel,
)
from app.schemas.catalog import (
    AllergenNotice,
    CompatibilityConflict,
    DietaryClaim,
    ExplorePage,
    ExploreVideo,
    Ingredient,
    Media,
    MenuCategory,
    MenuCompatibility,
    MenuItemDetail,
    MenuItemSummary,
    Money,
    OpeningHours,
    RestaurantDetail,
    RestaurantMenu,
    RestaurantPage,
    RestaurantSummary,
    Review,
    ReviewPage,
    SearchFacetOption,
    SearchFacets,
    SearchFacetSection,
    SearchResults,
    TrendingSearch,
    TrendingSearches,
)
from app.utils import decode_cursor, encode_cursor, haversine_meters, localized, normalize_locale


def _fold(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _codes(values: Sequence[str] | None) -> set[str]:
    return {
        _fold(value).replace("_", "-").replace(" ", "-") for value in values or () if value.strip()
    }


def _translation_for(translations: Iterable[Any], locale: str) -> Any | None:
    normalized = normalize_locale(locale)
    translations = list(translations)
    exact = next((item for item in translations if item.locale == normalized), None)
    if exact:
        return exact
    language = normalized.split("-", 1)[0]
    return next(
        (item for item in translations if item.locale.split("-", 1)[0] == language),
        None,
    )


def _restaurant_text(restaurant: RestaurantModel, locale: str) -> tuple[str, str, str]:
    translation = _translation_for(restaurant.translations, locale)
    if translation:
        return translation.name, translation.description, translation.address
    return (
        localized(restaurant.name_en, restaurant.name_ko, locale),
        localized(restaurant.description_en, restaurant.description_ko, locale),
        localized(restaurant.address_en, restaurant.address_ko, locale),
    )


def _category_name(category: MenuCategoryModel, locale: str) -> str:
    translation = _translation_for(category.translations, locale)
    if translation:
        return translation.name
    return localized(category.name_en, category.name_ko, locale)


def _item_text(item: MenuItemModel, locale: str) -> tuple[str, str, str | None]:
    translation = _translation_for(item.translations, locale)
    if translation:
        return translation.name, translation.description, translation.pronunciation
    return (
        localized(item.name_en, item.name_ko, locale),
        localized(item.description_en, item.description_ko, locale),
        item.pronunciation,
    )


def _media(raw_media: list[dict[str, Any]] | None) -> list[Media]:
    result: list[Media] = []
    for raw in raw_media or []:
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            continue
        duration = raw.get("duration_seconds")
        result.append(
            Media(
                kind=str(raw.get("kind") or "image"),
                url=url,
                thumbnail_url=raw.get("thumbnail_url"),
                duration_seconds=duration if isinstance(duration, int) else None,
                provider=raw.get("provider"),
                provider_video_id=raw.get("provider_video_id"),
                alt_text=raw.get("alt_text"),
            )
        )
    return result


def _money(amount: int, currency: str) -> Money:
    formatted = f"₩{amount:,}" if currency == "KRW" else f"{amount:,} {currency}"
    return Money(amount=amount, currency=currency, formatted=formatted)


_POSITIVE_ALLERGEN_RELATIONS = {"contains", "may_contain", "cross_contact"}
_NEGATIVE_ALLERGEN_RELATIONS = {"free_from", "does_not_contain"}
_DIET_IMPLICATIONS = {
    "vegan": {"vegan", "vegetarian", "pescatarian"},
    "vegetarian": {"vegetarian", "pescatarian"},
    "pescatarian": {"pescatarian"},
    "halal": {"halal"},
}
_DIET_COVERS_INGREDIENT = {
    "pork": {"vegan", "vegetarian", "pescatarian", "halal"},
    "pork-belly": {"vegan", "vegetarian", "pescatarian", "halal"},
    "meat": {"vegan", "vegetarian", "pescatarian"},
    "beef": {"vegan", "vegetarian", "pescatarian"},
    "chicken": {"vegan", "vegetarian", "pescatarian"},
    "dairy": {"vegan"},
    "milk": {"vegan"},
    "cheese": {"vegan"},
    "seafood": {"vegan", "vegetarian"},
    "fish": {"vegan", "vegetarian"},
    "shellfish": {"vegan", "vegetarian"},
}
_INGREDIENT_DESCENDANTS = {
    "allium": {"allium", "garlic", "onion", "scallion", "chive"},
    "dairy": {"dairy", "milk", "cheese"},
    "fish": {"fish", "fish-cake", "mackerel"},
    "gluten": {"gluten", "wheat", "wheat-flour"},
    "meat": {"meat", "pork", "pork-belly", "beef", "chicken"},
    "pork": {"pork", "pork-belly"},
    "seafood": {"seafood", "fish", "fish-cake", "mackerel", "squid", "shrimp", "abalone"},
    "sesame": {"sesame", "sesame-oil"},
    "shellfish": {"shellfish", "squid", "shrimp", "abalone"},
    "soy": {"soy", "soybean", "tofu"},
}


def _expanded_diet_claims(item: MenuItemModel) -> tuple[set[str], dict[str, str]]:
    expanded: set[str] = set()
    verification: dict[str, str] = {}
    for claim in item.dietary_claims:
        code = _fold(claim.code).replace(" ", "-")
        implied = _DIET_IMPLICATIONS.get(code, {code})
        expanded.update(implied)
        for implied_code in implied:
            previous = verification.get(implied_code)
            if previous is None or (
                previous in {"unknown", "unverified"}
                and claim.verification_status not in {"unknown", "unverified"}
            ):
                verification[implied_code] = claim.verification_status
    return expanded, verification


def _compatibility(item: MenuItemModel, locale: str, user: UserModel | None) -> MenuCompatibility:
    passport = user.passport if user is not None else None
    if passport is None:
        return MenuCompatibility(
            status="unknown",
            disclaimer=(
                "No food passport was applied. Ingredient and allergen information is "
                "merchant-reported and is not a medical guarantee."
            ),
        )

    diet_codes = _codes(passport.diet_codes)
    avoided_allergens = _codes(passport.avoid_allergen_codes)
    avoided_ingredients = _codes(passport.avoid_ingredient_codes)
    expanded_claims, claim_verification = _expanded_diet_claims(item)
    supported_claims = {
        code
        for code in expanded_claims
        if claim_verification.get(code) not in {"unknown", "unverified", None}
    }
    conflicts: list[CompatibilityConflict] = []
    missing_evidence: list[str] = []

    allergen_links_by_code: dict[str, list[MenuItemAllergen]] = {}
    for link in item.allergen_links:
        code = _fold(link.allergen_code).replace(" ", "-")
        allergen_links_by_code.setdefault(code, []).append(link)
        if code in avoided_allergens and link.relation_type in _POSITIVE_ALLERGEN_RELATIONS:
            conflicts.append(
                CompatibilityConflict(
                    code=code,
                    kind="allergen",
                    relation=link.relation_type,
                    verification_status=link.verification_status,
                    label=localized(link.allergen.name_en, link.allergen.name_ko, locale),
                )
            )

    ingredient_links = {
        _fold(link.ingredient_code).replace(" ", "-"): link for link in item.ingredient_links
    }
    for code in sorted(avoided_ingredients):
        descendants = _INGREDIENT_DESCENDANTS.get(code, {code})
        matching_codes = sorted(descendants.intersection(ingredient_links))
        link = ingredient_links[matching_codes[0]] if matching_codes else None
        if link:
            conflicts.append(
                CompatibilityConflict(
                    code=code,
                    kind="ingredient",
                    relation="contains",
                    verification_status="menu_declared",
                    label=localized(link.ingredient.name_en, link.ingredient.name_ko, locale),
                )
            )

    for code in sorted(diet_codes):
        if code not in supported_claims:
            missing_evidence.append(f"diet:{code}")

    conflict_allergen_codes = {
        conflict.code for conflict in conflicts if conflict.kind == "allergen"
    }
    for code in sorted(avoided_allergens - conflict_allergen_codes):
        links = allergen_links_by_code.get(code, [])
        has_negative_evidence = any(
            link.relation_type in _NEGATIVE_ALLERGEN_RELATIONS
            and link.verification_status not in {"unknown", "unverified"}
            for link in links
        )
        if not has_negative_evidence:
            missing_evidence.append(f"allergen:{code}")

    conflict_ingredient_codes = {
        conflict.code for conflict in conflicts if conflict.kind == "ingredient"
    }
    for code in sorted(avoided_ingredients - conflict_ingredient_codes):
        covering_diets = _DIET_COVERS_INGREDIENT.get(code, set())
        if not covering_diets.intersection(supported_claims):
            missing_evidence.append(f"ingredient:{code}")

    if conflicts:
        compatibility_status = "conflict"
    elif missing_evidence:
        compatibility_status = "unknown"
    else:
        compatibility_status = "compatible"
    return MenuCompatibility(
        status=compatibility_status,
        matched_conflicts=conflicts,
        missing_evidence=missing_evidence,
        disclaimer=(
            "Compatibility uses explicit merchant-reported claims only. Recipes and "
            "cross-contact can change; confirm severe allergies with the restaurant."
        ),
    )


def serialize_menu_item(
    item: MenuItemModel, locale: str, user: UserModel | None = None
) -> MenuItemSummary:
    name, description, pronunciation = _item_text(item, locale)
    ingredients = sorted(
        item.ingredient_links, key=lambda link: (link.sort_order, link.ingredient_code)
    )
    allergens = sorted(
        item.allergen_links, key=lambda link: (link.allergen_code, link.relation_type)
    )
    claims = sorted(item.dietary_claims, key=lambda claim: claim.code)
    return MenuItemSummary(
        id=item.id,
        restaurant_id=item.restaurant_id,
        category_id=item.category_id,
        slug=item.slug,
        name=name,
        original_name=item.name_ko if item.name_ko and item.name_ko != name else None,
        pronunciation=pronunciation,
        description=description,
        price=(_money(item.price_amount, item.currency) if item.price_amount is not None else None),
        serving_description=item.serving_description,
        spice_level=item.spice_level,
        badge=item.badge,
        image_url=item.image_url,
        media=_media(item.media),
        is_available=item.is_available,
        # Fofu prepares a language aid rather than charging the customer. A
        # missing menu price must therefore not prevent the dish from being
        # selected; availability remains the ordering gate.
        is_orderable=item.is_available,
        orderability_reason=(
            None
            if item.is_available and item.price_amount is not None
            else "unavailable"
            if not item.is_available
            else "price_unknown"
        ),
        ingredients=[
            Ingredient(
                code=link.ingredient_code,
                name=localized(link.ingredient.name_en, link.ingredient.name_ko, locale),
                emoji=link.ingredient.emoji,
                detail=localized(link.detail_en or "", link.detail_ko, locale) or None,
                is_primary=link.is_primary,
            )
            for link in ingredients
        ],
        allergens=[
            AllergenNotice(
                code=link.allergen_code,
                name=localized(link.allergen.name_en, link.allergen.name_ko, locale),
                relationship=link.relation_type,
                verification_status=link.verification_status,
                source=link.source,
            )
            for link in allergens
        ],
        dietary_claims=[
            DietaryClaim(code=claim.code, verification_status=claim.verification_status)
            for claim in claims
        ],
        compatibility=_compatibility(item, locale, user),
    )


def _restaurant_options() -> tuple[Any, ...]:
    category_items = selectinload(RestaurantModel.menu_categories).selectinload(
        MenuCategoryModel.items
    )
    return (
        selectinload(RestaurantModel.translations),
        selectinload(RestaurantModel.hours),
        selectinload(RestaurantModel.menu_categories).selectinload(MenuCategoryModel.translations),
        category_items.selectinload(MenuItemModel.translations),
        category_items.selectinload(MenuItemModel.ingredient_links).selectinload(
            MenuItemIngredient.ingredient
        ),
        category_items.selectinload(MenuItemModel.allergen_links).selectinload(
            MenuItemAllergen.allergen
        ),
        category_items.selectinload(MenuItemModel.dietary_claims),
    )


def _item_options() -> tuple[Any, ...]:
    return (
        selectinload(MenuItemModel.translations),
        selectinload(MenuItemModel.ingredient_links).selectinload(MenuItemIngredient.ingredient),
        selectinload(MenuItemModel.allergen_links).selectinload(MenuItemAllergen.allergen),
        selectinload(MenuItemModel.dietary_claims),
        selectinload(MenuItemModel.category).selectinload(MenuCategoryModel.translations),
    )


def _all_items(restaurant: RestaurantModel) -> list[MenuItemModel]:
    categories = sorted(
        (category for category in restaurant.menu_categories if category.is_active),
        key=lambda category: (category.sort_order, category.id),
    )
    return [
        item
        for category in categories
        for item in sorted(category.items, key=lambda value: (value.sort_order, value.id))
        if item.is_available
    ]


_MAIN_INGREDIENT_GROUP_ALIASES = {
    "seafood": {
        "seafood",
        "fish",
        "fish-cake",
        "mackerel",
        "squid",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "octopus",
        "abalone",
        "clam",
        "oyster",
        "shellfish",
        "salmon",
        "tuna",
    },
    "beef": {"beef", "steak", "short-rib", "galbi", "bulgogi"},
    "chicken": {"chicken", "poultry", "dak"},
    "vegetables": {
        "vegetable",
        "vegetables",
        "veggie",
        "spinach",
        "carrot",
        "mushroom",
        "bell-pepper",
        "pepper",
        "cucumber",
        "sweet-potato",
        "onion",
        "radish",
        "scallion",
        "chive",
        "garlic",
        "perilla",
        "kimchi",
        "lettuce",
        "cabbage",
        "broccoli",
        "zucchini",
        "eggplant",
    },
}

_DISH_TYPE_GROUP_ALIASES = {
    "bbq-grilled": {
        "bbq",
        "barbecue",
        "grill",
        "grilled",
        "grill-your-own",
        "samgyeopsal",
        "galbi",
    },
    "soup-stew": {"soup", "stew", "stews", "jjigae", "guk", "tang", "hot-pot"},
    "noodles": {"noodle", "noodles", "guksu", "ramen", "ramyeon", "japchae"},
    "rice-dishes": {
        "rice-dish",
        "rice-dishes",
        "rice-bowl",
        "rice-bowls",
        "bibimbap",
        "porridge",
        "juk",
    },
}

_PRICE_BUCKETS: dict[str, tuple[int | None, int | None]] = {
    "under-10000": (None, 10_000),
    "10000-20000": (10_000, 20_000),
    "20000-35000": (20_000, 35_000),
    "35000-plus": (35_000, None),
}

_CURATED_TASTE_CODES = ("not-spicy", "mild", "rich", "light", "crispy")
_LARGE_CATALOG_ITEM_THRESHOLD = 100_000


def _catalog_is_large(db: Session) -> bool:
    """Use PostgreSQL planner statistics to avoid a count scan on every request."""

    if db.get_bind().dialect.name == "postgresql":
        estimate = db.scalar(
            text("SELECT reltuples::bigint FROM pg_class WHERE oid = 'menu_items'::regclass")
        )
        if estimate is not None:
            return int(estimate) >= _LARGE_CATALOG_ITEM_THRESHOLD
    count = db.scalar(select(func.count(MenuItemModel.id)))
    return int(count or 0) >= _LARGE_CATALOG_ITEM_THRESHOLD


def _sql_normalized_text(value: Any) -> Any:
    """Apply the database-portable part of ``_fold`` to a text expression."""

    return func.lower(func.replace(func.replace(func.coalesce(value, ""), "_", "-"), " ", "-"))


def _sql_contains_any_alias(values: Sequence[Any], aliases: set[str]) -> Any:
    predicates = []
    for value in values:
        padded = literal("-") + _sql_normalized_text(value) + literal("-")
        predicates.extend(padded.like(f"%-{alias}-%") for alias in aliases)
    return or_(*predicates)


def _sql_main_ingredient_predicate(code: str) -> Any:
    link_match: Any
    aliases = _MAIN_INGREDIENT_GROUP_ALIASES.get(code)
    if aliases is None:
        link_match = _sql_normalized_text(MenuItemIngredient.ingredient_code) == code
        from_clause = MenuItemIngredient
    else:
        link_match = _sql_contains_any_alias(
            (
                MenuItemIngredient.ingredient_code,
                IngredientModel.name_en,
                IngredientModel.name_ko,
            ),
            aliases,
        )
        from_clause = MenuItemIngredient.__table__.join(
            IngredientModel,
            IngredientModel.code == MenuItemIngredient.ingredient_code,
        )
    return exists(
        select(1)
        .select_from(from_clause)
        .where(
            MenuItemIngredient.menu_item_id == MenuItemModel.id,
            link_match,
        )
        .correlate(MenuItemModel)
    )


def _sql_dish_type_predicate(code: str) -> Any:
    aliases = _DISH_TYPE_GROUP_ALIASES.get(code)
    if aliases is None:
        return or_(
            *(
                _sql_normalized_text(value).contains(code, autoescape=True)
                for value in (
                    MenuCategoryModel.slug,
                    MenuCategoryModel.name_en,
                    MenuCategoryModel.name_ko,
                )
            )
        )

    base_match = _sql_contains_any_alias(
        (
            MenuCategoryModel.slug,
            MenuCategoryModel.name_en,
            MenuCategoryModel.name_ko,
            MenuItemModel.slug,
            MenuItemModel.name_en,
            MenuItemModel.name_ko,
            MenuItemModel.description_en,
            MenuItemModel.description_ko,
        ),
        aliases,
    )
    category_translation_match = exists(
        select(1)
        .select_from(MenuCategoryTranslationModel)
        .where(
            MenuCategoryTranslationModel.category_id == MenuCategoryModel.id,
            _sql_contains_any_alias((MenuCategoryTranslationModel.name,), aliases),
        )
        .correlate(MenuCategoryModel)
    )
    item_translation_match = exists(
        select(1)
        .select_from(MenuItemTranslationModel)
        .where(
            MenuItemTranslationModel.menu_item_id == MenuItemModel.id,
            _sql_contains_any_alias(
                (
                    MenuItemTranslationModel.name,
                    MenuItemTranslationModel.description,
                ),
                aliases,
            ),
        )
        .correlate(MenuItemModel)
    )
    return or_(base_match, category_translation_match, item_translation_match)


def _sql_taste_predicate(code: str) -> Any:
    if code == "not-spicy":
        return MenuItemModel.spice_level == 0
    if code == "mild":
        return MenuItemModel.spice_level <= 1
    return MenuItemModel.taste_profile[code].as_float() >= 0.5


def _sql_solo_friendly_predicate() -> Any:
    raw = func.lower(func.coalesce(MenuItemModel.serving_description, ""))
    normalized_words = raw
    for separator in ("-", "_", "/", ",", ".", "(", ")"):
        normalized_words = func.replace(normalized_words, separator, " ")
    padded_words = literal(" ") + normalized_words + literal(" ")
    word_matches = [
        padded_words.like(f"% {word} %")
        for word in ("one", "single", "solo", "individual", "personal")
    ]
    marker_matches = [
        raw.contains(marker, autoescape=True)
        for marker in ("1-person", "for-one", "serves-one", "one-person", "1인", "일인")
    ]
    return or_(*word_matches, *marker_matches)


def _sql_item_filter_predicates(
    *,
    diet_codes: set[str],
    excluded_allergen_codes: set[str],
    ingredient_codes: set[str],
    spicy: bool | None,
    max_price: int | None,
    main_ingredient_codes: set[str],
    dish_type_codes: set[str],
    price_codes: set[str],
    taste_codes: set[str],
    solo_friendly: bool,
    require_taste_evidence: bool = False,
    require_allergen_evidence: bool = False,
    treat_zero_spice_as_unknown: bool = False,
    require_solo_evidence: bool = False,
) -> list[Any]:
    predicates: list[Any] = []
    for code in diet_codes:
        source_codes = {
            source for source, implications in _DIET_IMPLICATIONS.items() if code in implications
        }
        source_codes.add(code)
        predicates.append(
            exists(
                select(1)
                .select_from(MenuItemDietaryClaim)
                .where(
                    MenuItemDietaryClaim.menu_item_id == MenuItemModel.id,
                    _sql_normalized_text(MenuItemDietaryClaim.code).in_(source_codes),
                )
                .correlate(MenuItemModel)
            )
        )
    if excluded_allergen_codes:
        if require_allergen_evidence:
            for code in excluded_allergen_codes:
                predicates.append(
                    exists(
                        select(1)
                        .select_from(MenuItemAllergen)
                        .where(
                            MenuItemAllergen.menu_item_id == MenuItemModel.id,
                            MenuItemAllergen.relation_type.in_(_NEGATIVE_ALLERGEN_RELATIONS),
                            MenuItemAllergen.verification_status.not_in(("unknown", "unverified")),
                            _sql_normalized_text(MenuItemAllergen.allergen_code) == code,
                        )
                        .correlate(MenuItemModel)
                    )
                )
        else:
            predicates.append(
                ~exists(
                    select(1)
                    .select_from(MenuItemAllergen)
                    .where(
                        MenuItemAllergen.menu_item_id == MenuItemModel.id,
                        MenuItemAllergen.relation_type.in_(_POSITIVE_ALLERGEN_RELATIONS),
                        _sql_normalized_text(MenuItemAllergen.allergen_code).in_(
                            excluded_allergen_codes
                        ),
                    )
                    .correlate(MenuItemModel)
                )
            )
    for code in ingredient_codes:
        predicates.append(
            exists(
                select(1)
                .select_from(MenuItemIngredient)
                .where(
                    MenuItemIngredient.menu_item_id == MenuItemModel.id,
                    _sql_normalized_text(MenuItemIngredient.ingredient_code) == code,
                )
                .correlate(MenuItemModel)
            )
        )
    if spicy is True:
        predicates.append(MenuItemModel.spice_level > 0)
    elif spicy is False:
        predicates.append(
            literal(False) if treat_zero_spice_as_unknown else MenuItemModel.spice_level <= 0
        )
    if max_price is not None:
        predicates.extend(
            (
                MenuItemModel.price_amount.is_not(None),
                MenuItemModel.price_amount <= max_price,
            )
        )
    if main_ingredient_codes:
        predicates.append(
            or_(*(_sql_main_ingredient_predicate(code) for code in main_ingredient_codes))
        )
    if dish_type_codes:
        predicates.append(or_(*(_sql_dish_type_predicate(code) for code in dish_type_codes)))
    if price_codes:
        bucket_predicates = []
        for code in price_codes:
            bounds = _PRICE_BUCKETS.get(code)
            if bounds is None:
                continue
            minimum, maximum_exclusive = bounds
            bucket = [MenuItemModel.price_amount.is_not(None)]
            if minimum is not None:
                bucket.append(MenuItemModel.price_amount >= minimum)
            if maximum_exclusive is not None:
                bucket.append(MenuItemModel.price_amount < maximum_exclusive)
            bucket_predicates.append(and_(*bucket))
        predicates.append(or_(*bucket_predicates) if bucket_predicates else literal(False))
    if taste_codes:
        if require_taste_evidence:
            predicates.append(literal(False))
        else:
            predicates.append(or_(*(_sql_taste_predicate(code) for code in taste_codes)))
    if solo_friendly:
        predicates.append(
            literal(False) if require_solo_evidence else _sql_solo_friendly_predicate()
        )
    return predicates


def _sql_open_now_predicate(
    db: Session,
    now: datetime,
    *,
    require_hours: bool = False,
) -> Any:
    no_hours = ~exists(
        select(1)
        .select_from(OpeningHourModel)
        .where(OpeningHourModel.restaurant_id == RestaurantModel.id)
        .correlate(RestaurantModel)
    )
    timezone_names = list(
        db.scalars(
            select(RestaurantModel.timezone_name)
            .where(RestaurantModel.is_published.is_(True))
            .distinct()
        )
    )
    timezone_predicates = []
    for timezone_name in timezone_names:
        try:
            local_now = now.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            local_now = now
        today_index = local_now.weekday()
        current_time = local_now.time().replace(tzinfo=None)
        today = exists(
            select(1)
            .select_from(OpeningHourModel)
            .where(
                OpeningHourModel.restaurant_id == RestaurantModel.id,
                OpeningHourModel.day_of_week == today_index,
                OpeningHourModel.is_closed.is_(False),
                OpeningHourModel.opens_at.is_not(None),
                OpeningHourModel.closes_at.is_not(None),
                or_(
                    OpeningHourModel.opens_at == OpeningHourModel.closes_at,
                    and_(
                        OpeningHourModel.closes_at > OpeningHourModel.opens_at,
                        OpeningHourModel.opens_at <= current_time,
                        OpeningHourModel.closes_at > current_time,
                    ),
                    and_(
                        OpeningHourModel.closes_at < OpeningHourModel.opens_at,
                        OpeningHourModel.opens_at <= current_time,
                    ),
                ),
            )
            .correlate(RestaurantModel)
        )
        previous = exists(
            select(1)
            .select_from(OpeningHourModel)
            .where(
                OpeningHourModel.restaurant_id == RestaurantModel.id,
                OpeningHourModel.day_of_week == (today_index - 1) % 7,
                OpeningHourModel.is_closed.is_(False),
                OpeningHourModel.opens_at.is_not(None),
                OpeningHourModel.closes_at.is_not(None),
                OpeningHourModel.closes_at < OpeningHourModel.opens_at,
                OpeningHourModel.closes_at > current_time,
            )
            .correlate(RestaurantModel)
        )
        timezone_predicates.append(
            and_(RestaurantModel.timezone_name == timezone_name, or_(today, previous))
        )
    scheduled_open = or_(*timezone_predicates) if timezone_predicates else literal(False)
    hours_match = scheduled_open if require_hours else or_(no_hours, scheduled_open)
    return and_(RestaurantModel.is_open.is_(True), hours_match)


def _contains_alias(value: str, aliases: set[str]) -> bool:
    normalized = _fold(value).replace("_", "-").replace(" ", "-")
    padded = f"-{normalized}-"
    return any(alias == normalized or f"-{alias}-" in padded for alias in aliases)


def _item_matches_main_ingredients(item: MenuItemModel, codes: set[str]) -> bool:
    if not codes:
        return True
    ingredient_codes = {
        _fold(link.ingredient_code).replace("_", "-").replace(" ", "-")
        for link in item.ingredient_links
    }
    ingredient_values = [
        value
        for link in item.ingredient_links
        for value in (
            link.ingredient_code,
            link.ingredient.name_en,
            link.ingredient.name_ko or "",
        )
    ]
    for code in codes:
        aliases = _MAIN_INGREDIENT_GROUP_ALIASES.get(code)
        if aliases is not None:
            if any(_contains_alias(value, aliases) for value in ingredient_values):
                return True
        elif code in ingredient_codes:
            return True
    return False


def _dish_type_blob(category: MenuCategoryModel, item: MenuItemModel) -> str:
    values = [
        category.slug,
        category.name_en,
        category.name_ko or "",
        item.slug,
        item.name_en,
        item.name_ko or "",
        item.description_en,
        item.description_ko or "",
        *(translation.name for translation in category.translations),
        *(translation.name for translation in item.translations),
        *(translation.description or "" for translation in item.translations),
    ]
    return _fold(" ".join(values)).replace("_", "-")


def _item_matches_dish_types(
    item: MenuItemModel,
    category: MenuCategoryModel,
    codes: set[str],
) -> bool:
    if not codes:
        return True
    category_blob = _fold(
        " ".join(value for value in (category.slug, category.name_en, category.name_ko) if value)
    ).replace("_", "-")
    item_blob = _dish_type_blob(category, item)
    for code in codes:
        aliases = _DISH_TYPE_GROUP_ALIASES.get(code)
        if aliases is not None:
            if any(_contains_alias(item_blob, {alias}) for alias in aliases):
                return True
        elif _fold(code).replace("_", "-").replace(" ", "-") in category_blob.replace(" ", "-"):
            return True
    return False


def _item_matches_price_codes(item: MenuItemModel, codes: set[str]) -> bool:
    if not codes:
        return True
    if item.price_amount is None:
        return False
    for code in codes:
        bounds = _PRICE_BUCKETS.get(code)
        if bounds is None:
            continue
        minimum, maximum_exclusive = bounds
        if minimum is not None and item.price_amount < minimum:
            continue
        if maximum_exclusive is not None and item.price_amount >= maximum_exclusive:
            continue
        return True
    return False


def _item_matches_taste_code(item: MenuItemModel, code: str) -> bool:
    if code == "not-spicy":
        return item.spice_level == 0
    if code == "mild":
        return item.spice_level <= 1
    profile = {
        _fold(key).replace("_", "-").replace(" ", "-"): value
        for key, value in (item.taste_profile or {}).items()
    }
    return profile.get(code, 0) >= 0.5


def _item_matches_taste_codes(item: MenuItemModel, codes: set[str]) -> bool:
    return not codes or any(_item_matches_taste_code(item, code) for code in codes)


def _item_is_solo_friendly(item: MenuItemModel) -> bool:
    serving = _fold(item.serving_description or "").replace("_", "-")
    words = set(re.findall(r"[\w]+", serving, flags=re.UNICODE))
    if words.intersection({"one", "single", "solo", "individual", "personal"}):
        return True
    return any(
        marker in serving
        for marker in ("1-person", "for-one", "serves-one", "one-person", "1인", "일인")
    )


def _item_matches_filters(
    item: MenuItemModel,
    *,
    diet_codes: set[str],
    excluded_allergen_codes: set[str],
    ingredient_codes: set[str],
    spicy: bool | None,
    max_price: int | None = None,
    taste: str | None = None,
    main_ingredient_codes: set[str] | None = None,
    dish_type_codes: set[str] | None = None,
    price_codes: set[str] | None = None,
    taste_codes: set[str] | None = None,
    solo_friendly: bool = False,
    category: MenuCategoryModel | None = None,
) -> bool:
    claims, _ = _expanded_diet_claims(item)
    allergens = {
        _fold(link.allergen_code).replace(" ", "-")
        for link in item.allergen_links
        if link.relation_type in _POSITIVE_ALLERGEN_RELATIONS
    }
    ingredients = {_fold(link.ingredient_code).replace(" ", "-") for link in item.ingredient_links}
    if diet_codes and not diet_codes.issubset(claims):
        return False
    if excluded_allergen_codes.intersection(allergens):
        return False
    if ingredient_codes and not ingredient_codes.issubset(ingredients):
        return False
    if spicy is True and item.spice_level <= 0:
        return False
    if spicy is False and item.spice_level > 0:
        return False
    if max_price is not None:
        if item.price_amount is None or item.price_amount > max_price:
            return False

    main_ingredient_codes = main_ingredient_codes or set()
    dish_type_codes = dish_type_codes or set()
    price_codes = price_codes or set()
    taste_codes = set(taste_codes or ())
    if taste:
        taste_codes.update(_codes((taste,)))
    item_category = category or item.category
    if not _item_matches_main_ingredients(item, main_ingredient_codes):
        return False
    if not _item_matches_dish_types(item, item_category, dish_type_codes):
        return False
    if not _item_matches_price_codes(item, price_codes):
        return False
    if not _item_matches_taste_codes(item, taste_codes):
        return False
    if solo_friendly and not _item_is_solo_friendly(item):
        return False

    return True


def _matching_items(
    restaurant: RestaurantModel,
    *,
    diet_codes: set[str],
    excluded_allergen_codes: set[str],
    ingredient_codes: set[str],
    spicy: bool | None,
    max_price: int | None = None,
    taste: str | None = None,
    main_ingredient_codes: set[str] | None = None,
    dish_type_codes: set[str] | None = None,
    price_codes: set[str] | None = None,
    taste_codes: set[str] | None = None,
    solo_friendly: bool = False,
) -> list[MenuItemModel]:
    return [
        item
        for item in _all_items(restaurant)
        if _item_matches_filters(
            item,
            diet_codes=diet_codes,
            excluded_allergen_codes=excluded_allergen_codes,
            ingredient_codes=ingredient_codes,
            spicy=spicy,
            max_price=max_price,
            taste=taste,
            main_ingredient_codes=main_ingredient_codes,
            dish_type_codes=dish_type_codes,
            price_codes=price_codes,
            taste_codes=taste_codes,
            solo_friendly=solo_friendly,
            category=item.category,
        )
    ]


def restaurant_is_open(restaurant: RestaurantModel, now: datetime | None = None) -> bool:
    if not restaurant.is_open:
        return False
    if not restaurant.hours:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        local_now = now.astimezone(ZoneInfo(restaurant.timezone_name))
    except ZoneInfoNotFoundError:
        local_now = now

    hours_by_day = {entry.day_of_week: entry for entry in restaurant.hours}
    today_index = local_now.weekday()
    current_time = local_now.time().replace(tzinfo=None)
    today = hours_by_day.get(today_index)
    if today and not today.is_closed and today.opens_at is not None and today.closes_at is not None:
        if today.opens_at == today.closes_at:
            return True
        if today.closes_at > today.opens_at and today.opens_at <= current_time < today.closes_at:
            return True
        if today.closes_at < today.opens_at and current_time >= today.opens_at:
            return True

    previous = hours_by_day.get((today_index - 1) % 7)
    return bool(
        previous
        and not previous.is_closed
        and previous.opens_at is not None
        and previous.closes_at is not None
        and previous.closes_at < previous.opens_at
        and current_time < previous.closes_at
    )


def serialize_restaurant(
    restaurant: RestaurantModel,
    locale: str,
    *,
    distance_m: int | None = None,
    featured_item: MenuItemModel | None = None,
    user: UserModel | None = None,
) -> RestaurantSummary:
    name, description, _ = _restaurant_text(restaurant, locale)
    if featured_item is None:
        featured_item = next(iter(_all_items(restaurant)), None)
    return RestaurantSummary(
        id=restaurant.id,
        slug=restaurant.slug,
        name=name,
        description=description,
        handle=restaurant.handle,
        category=restaurant.category,
        hero_style=restaurant.hero_style,
        latitude=restaurant.latitude,
        longitude=restaurant.longitude,
        distance_m=distance_m,
        rating_avg=float(restaurant.rating_avg),
        rating_count=restaurant.rating_count,
        is_verified=restaurant.is_verified,
        is_open_now=restaurant_is_open(restaurant),
        cover_image_url=restaurant.cover_image_url,
        featured_item=serialize_menu_item(featured_item, locale, user) if featured_item else None,
    )


def _load_restaurants(
    db: Session,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_m: int | None = None,
) -> list[RestaurantModel]:
    statement = select(RestaurantModel).where(RestaurantModel.is_published.is_(True))
    if latitude is not None and longitude is not None and radius_m is not None:
        latitude_delta = radius_m / 111_320
        longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.01)
        longitude_delta = radius_m / (111_320 * longitude_scale)
        statement = statement.where(
            RestaurantModel.latitude.between(latitude - latitude_delta, latitude + latitude_delta),
            RestaurantModel.longitude.between(
                longitude - longitude_delta, longitude + longitude_delta
            ),
        )
    statement = statement.options(*_restaurant_options())
    return list(db.scalars(statement).unique())


def _unfiltered_nearby_restaurants(
    db: Session,
    *,
    latitude: float,
    longitude: float,
    radius_m: int,
    locale: str,
    cursor: str | None,
    limit: int,
    user: UserModel | None,
) -> RestaurantPage:
    """Page nearby restaurants before loading their catalog relationships."""

    latitude_delta = radius_m / 111_320
    longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.01)
    longitude_delta = radius_m / (111_320 * longitude_scale)
    statement = select(
        RestaurantModel.id,
        RestaurantModel.latitude,
        RestaurantModel.longitude,
        RestaurantModel.rating_avg,
        RestaurantModel.slug,
    ).where(
        RestaurantModel.is_published.is_(True),
        RestaurantModel.latitude.between(latitude - latitude_delta, latitude + latitude_delta),
        RestaurantModel.longitude.between(longitude - longitude_delta, longitude + longitude_delta),
    )

    matches: list[tuple[int, float, str, str]] = []
    for restaurant_id, restaurant_latitude, restaurant_longitude, rating_avg, slug in db.execute(
        statement
    ):
        distance = haversine_meters(
            latitude,
            longitude,
            restaurant_latitude,
            restaurant_longitude,
        )
        if distance <= radius_m:
            matches.append((distance, float(rating_avg), slug, restaurant_id))

    matches.sort(key=lambda row: (row[0], -row[1], row[2]))
    offset = decode_cursor(cursor)
    page_candidates = matches[offset : offset + limit]
    next_offset = offset + len(page_candidates)
    has_more = next_offset < len(matches)

    if not page_candidates:
        return RestaurantPage(
            items=[],
            next_cursor=None,
            has_more=False,
            total=len(matches),
        )

    page_ids = [restaurant_id for _, _, _, restaurant_id in page_candidates]
    page_statement = (
        select(RestaurantModel)
        .where(RestaurantModel.id.in_(page_ids))
        .options(*_restaurant_options())
    )
    restaurants_by_id = {
        restaurant.id: restaurant for restaurant in db.scalars(page_statement).unique()
    }
    return RestaurantPage(
        items=[
            serialize_restaurant(
                restaurants_by_id[restaurant_id],
                locale,
                distance_m=distance,
                user=user,
            )
            for distance, _, _, restaurant_id in page_candidates
        ],
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
        total=len(matches),
    )


def nearby_restaurants(
    db: Session,
    *,
    latitude: float,
    longitude: float,
    radius_m: int,
    locale: str,
    diet_codes: Sequence[str] = (),
    excluded_allergen_codes: Sequence[str] = (),
    ingredient_codes: Sequence[str] = (),
    main_ingredient_codes: Sequence[str] = (),
    dish_type_codes: Sequence[str] = (),
    price_codes: Sequence[str] = (),
    taste_codes: Sequence[str] = (),
    spicy: bool | None = None,
    solo_friendly: bool = False,
    open_now: bool = False,
    min_rating: float | None = None,
    category: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
    user: UserModel | None = None,
) -> RestaurantPage:
    diets = _codes(diet_codes)
    excluded = _codes(excluded_allergen_codes)
    ingredients = _codes(ingredient_codes)
    main_ingredients = _codes(main_ingredient_codes)
    dish_types = _codes(dish_type_codes)
    prices = _codes(price_codes)
    tastes = _codes(taste_codes)
    large_catalog = _catalog_is_large(db)
    has_item_filters = bool(
        diets
        or excluded
        or ingredients
        or main_ingredients
        or dish_types
        or prices
        or tastes
        or spicy is not None
        or solo_friendly
    )
    normalized_category = unicodedata.normalize("NFC", (category or "").strip().casefold())
    if (
        not large_catalog
        and not has_item_filters
        and not open_now
        and min_rating is None
        and not normalized_category
    ):
        return _unfiltered_nearby_restaurants(
            db,
            latitude=latitude,
            longitude=longitude,
            radius_m=radius_m,
            locale=locale,
            cursor=cursor,
            limit=limit,
            user=user,
        )

    latitude_delta = radius_m / 111_320
    longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.01)
    longitude_delta = radius_m / (111_320 * longitude_scale)
    distance_expression = _sql_distance_meters(latitude, longitude)
    restaurant_predicates: list[Any] = [
        RestaurantModel.is_published.is_(True),
        RestaurantModel.latitude.between(
            latitude - latitude_delta,
            latitude + latitude_delta,
        ),
        RestaurantModel.longitude.between(
            longitude - longitude_delta,
            longitude + longitude_delta,
        ),
        distance_expression <= radius_m,
    ]
    if open_now:
        restaurant_predicates.append(
            _sql_open_now_predicate(
                db,
                datetime.now(timezone.utc),
                require_hours=large_catalog,
            )
        )
    if min_rating is not None:
        restaurant_predicates.append(RestaurantModel.rating_avg >= min_rating)
    if normalized_category:
        restaurant_predicates.append(
            func.lower(RestaurantModel.category).contains(normalized_category, autoescape=True)
        )

    item_predicates = _sql_item_filter_predicates(
        diet_codes=diets,
        excluded_allergen_codes=excluded,
        ingredient_codes=ingredients,
        spicy=spicy,
        max_price=None,
        main_ingredient_codes=main_ingredients,
        dish_type_codes=dish_types,
        price_codes=prices,
        taste_codes=tastes,
        solo_friendly=solo_friendly,
        require_taste_evidence=large_catalog,
        require_allergen_evidence=large_catalog,
        treat_zero_spice_as_unknown=large_catalog,
        require_solo_evidence=large_catalog,
    )
    matching_item_from = MenuItemModel.__table__.join(
        MenuCategoryModel,
        MenuCategoryModel.id == MenuItemModel.category_id,
    )
    if has_item_filters:
        restaurant_predicates.append(
            exists(
                select(1)
                .select_from(matching_item_from)
                .where(
                    MenuItemModel.restaurant_id == RestaurantModel.id,
                    MenuCategoryModel.is_active.is_(True),
                    MenuItemModel.is_available.is_(True),
                    *item_predicates,
                )
                .correlate(RestaurantModel)
            )
        )

    offset = decode_cursor(cursor)
    page_rows = list(
        db.execute(
            select(
                RestaurantModel.id,
                distance_expression.label("distance_m"),
                func.count().over().label("total_count"),
            )
            .where(*restaurant_predicates)
            .order_by(
                distance_expression,
                RestaurantModel.rating_avg.desc(),
                RestaurantModel.slug,
            )
            .offset(offset)
            .limit(limit)
        )
    )
    if page_rows:
        total = int(page_rows[0].total_count)
    elif offset:
        total = int(
            db.scalar(select(func.count(RestaurantModel.id)).where(*restaurant_predicates)) or 0
        )
    else:
        total = 0
    next_offset = offset + len(page_rows)
    has_more = next_offset < total

    page_ids = [str(row.id) for row in page_rows]
    featured_item_id_by_restaurant: dict[str, str] = {}
    if page_ids:
        ranked_items = (
            select(
                MenuItemModel.id.label("item_id"),
                MenuItemModel.restaurant_id.label("restaurant_id"),
                func.row_number()
                .over(
                    partition_by=MenuItemModel.restaurant_id,
                    order_by=(
                        MenuCategoryModel.sort_order,
                        MenuCategoryModel.id,
                        MenuItemModel.sort_order,
                        MenuItemModel.id,
                    ),
                )
                .label("row_number"),
            )
            .select_from(matching_item_from)
            .where(
                MenuItemModel.restaurant_id.in_(page_ids),
                MenuCategoryModel.is_active.is_(True),
                MenuItemModel.is_available.is_(True),
                *(item_predicates if has_item_filters else ()),
            )
            .subquery()
        )
        featured_item_id_by_restaurant = {
            str(restaurant_id): str(item_id)
            for item_id, restaurant_id in db.execute(
                select(ranked_items.c.item_id, ranked_items.c.restaurant_id).where(
                    ranked_items.c.row_number == 1
                )
            )
        }

    featured_item_ids = list(featured_item_id_by_restaurant.values())
    featured_items = {
        item.id: item
        for item in db.scalars(
            select(MenuItemModel)
            .where(MenuItemModel.id.in_(featured_item_ids))
            .options(*_item_options())
        ).unique()
    }
    page_restaurants = {
        restaurant.id: restaurant
        for restaurant in db.scalars(
            select(RestaurantModel)
            .where(RestaurantModel.id.in_(page_ids))
            .options(
                selectinload(RestaurantModel.translations),
                selectinload(RestaurantModel.hours),
            )
        ).unique()
    }
    return RestaurantPage(
        items=[
            serialize_restaurant(
                page_restaurants[str(row.id)],
                locale,
                distance_m=int(row.distance_m),
                featured_item=(
                    featured_items.get(featured_item_id_by_restaurant.get(str(row.id), ""))
                ),
                user=user,
            )
            for row in page_rows
        ],
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
        total=total,
    )


def get_restaurant(db: Session, identifier: str) -> RestaurantModel:
    statement = (
        select(RestaurantModel)
        .where(
            RestaurantModel.is_published.is_(True),
            or_(RestaurantModel.id == identifier, RestaurantModel.slug == identifier),
        )
        .options(*_restaurant_options())
    )
    restaurant = db.scalars(statement).unique().first()
    if not restaurant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "restaurant_not_found", "message": "Restaurant not found."},
        )
    return restaurant


def restaurant_detail(
    db: Session,
    identifier: str,
    *,
    locale: str,
    latitude: float | None = None,
    longitude: float | None = None,
    user: UserModel | None = None,
) -> RestaurantDetail:
    restaurant = get_restaurant(db, identifier)
    _, _, address = _restaurant_text(restaurant, locale)
    distance = None
    if latitude is not None and longitude is not None:
        distance = haversine_meters(latitude, longitude, restaurant.latitude, restaurant.longitude)
    summary = serialize_restaurant(restaurant, locale, distance_m=distance, user=user)
    menu_items = _all_items(restaurant)
    return RestaurantDetail(
        **summary.model_dump(),
        address=address,
        original_address=(
            restaurant.address_ko
            if restaurant.address_ko and restaurant.address_ko != address
            else None
        ),
        phone=restaurant.phone,
        currency=restaurant.currency,
        timezone=restaurant.timezone_name,
        gallery=restaurant.gallery or [],
        hours=[
            OpeningHours(
                day_of_week=entry.day_of_week,
                opens_at=entry.opens_at,
                closes_at=entry.closes_at,
                is_closed=entry.is_closed,
            )
            for entry in sorted(restaurant.hours, key=lambda value: value.day_of_week)
        ],
        menu_category_count=sum(1 for value in restaurant.menu_categories if value.is_active),
        menu_item_count=len(menu_items),
    )


def restaurant_menu(
    db: Session,
    identifier: str,
    *,
    locale: str,
    user: UserModel | None = None,
) -> RestaurantMenu:
    restaurant = get_restaurant(db, identifier)
    categories = sorted(
        (category for category in restaurant.menu_categories if category.is_active),
        key=lambda value: (value.sort_order, value.id),
    )
    return RestaurantMenu(
        restaurant=serialize_restaurant(restaurant, locale, user=user),
        categories=[
            MenuCategory(
                id=category.id,
                slug=category.slug,
                name=_category_name(category, locale),
                sort_order=category.sort_order,
                items=[
                    serialize_menu_item(item, locale, user)
                    for item in sorted(
                        (item for item in category.items if item.is_available),
                        key=lambda value: (value.sort_order, value.id),
                    )
                ],
            )
            for category in categories
        ],
    )


def get_menu_item(
    db: Session,
    identifier: str,
    *,
    restaurant_identifier: str | None = None,
) -> MenuItemModel:
    statement = (
        select(MenuItemModel)
        .join(RestaurantModel, RestaurantModel.id == MenuItemModel.restaurant_id)
        .where(
            RestaurantModel.is_published.is_(True),
            or_(MenuItemModel.id == identifier, MenuItemModel.slug == identifier),
        )
        .options(*_item_options())
    )
    if restaurant_identifier:
        statement = statement.where(
            or_(
                RestaurantModel.id == restaurant_identifier,
                RestaurantModel.slug == restaurant_identifier,
            )
        )
    item = db.scalars(statement).unique().first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "menu_item_not_found", "message": "Menu item not found."},
        )
    return item


def menu_item_detail(
    db: Session,
    identifier: str,
    *,
    locale: str,
    restaurant_identifier: str | None = None,
    user: UserModel | None = None,
) -> MenuItemDetail:
    item = get_menu_item(db, identifier, restaurant_identifier=restaurant_identifier)
    count, average = db.execute(
        select(func.count(ReviewModel.id), func.avg(ReviewModel.rating)).where(
            ReviewModel.menu_item_id == item.id,
            ReviewModel.is_published.is_(True),
        )
    ).one()
    summary = serialize_menu_item(item, locale, user)
    return MenuItemDetail(
        **summary.model_dump(),
        taste_profile=item.taste_profile or {},
        local_tips=item.local_tips or [],
        review_count=int(count or 0),
        rating_avg=round(float(average), 2) if average is not None else None,
    )


def menu_item_reviews(
    db: Session,
    identifier: str,
    *,
    cursor: str | None,
    limit: int,
    restaurant_identifier: str | None = None,
) -> ReviewPage:
    item = get_menu_item(db, identifier, restaurant_identifier=restaurant_identifier)
    statement = (
        select(ReviewModel)
        .where(
            ReviewModel.menu_item_id == item.id,
            ReviewModel.is_published.is_(True),
        )
        .order_by(ReviewModel.created_at.desc(), ReviewModel.id.desc())
    )
    reviews = list(db.scalars(statement))
    offset = decode_cursor(cursor)
    page = reviews[offset : offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(reviews)
    return ReviewPage(
        items=[
            Review(
                id=review.id,
                menu_item_id=review.menu_item_id,
                rating=review.rating,
                body=review.body,
                author_display_name=review.author_display_name,
                author_country_code=review.author_country_code,
                tags=review.tags or [],
                created_at=review.created_at,
            )
            for review in page
        ],
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
        total=len(reviews),
    )


def _search_blob(
    restaurant: RestaurantModel,
    category: MenuCategoryModel,
    item: MenuItemModel,
    locale: str,
) -> str:
    restaurant_name, restaurant_description, _ = _restaurant_text(restaurant, locale)
    item_name, item_description, _ = _item_text(item, locale)
    values = [
        restaurant_name,
        restaurant.name_en,
        restaurant.name_ko or "",
        restaurant_description,
        restaurant.category,
        category.slug,
        category.name_en,
        category.name_ko or "",
        _category_name(category, locale),
        item.slug,
        item_name,
        item.name_en,
        item.name_ko or "",
        item_description,
        *(translation.name for translation in item.translations),
        *(translation.description for translation in item.translations),
        *(link.ingredient_code for link in item.ingredient_links),
        *(link.ingredient.name_en for link in item.ingredient_links),
        *(link.ingredient.name_ko or "" for link in item.ingredient_links),
        *(claim.code for claim in item.dietary_claims),
    ]
    return _fold(" ".join(values))


def _sql_text_match(values: Sequence[Any], query: str) -> Any:
    return or_(
        *(func.lower(func.coalesce(value, "")).contains(query, autoescape=True) for value in values)
    )


def _sql_localized_item_name(locale: str) -> Any:
    normalized = normalize_locale(locale)
    language = normalized.split("-", 1)[0]
    translated_name = (
        select(MenuItemTranslationModel.name)
        .where(
            MenuItemTranslationModel.menu_item_id == MenuItemModel.id,
            or_(
                MenuItemTranslationModel.locale == normalized,
                MenuItemTranslationModel.locale == language,
                MenuItemTranslationModel.locale.like(f"{language}-%"),
            ),
        )
        .order_by(case((MenuItemTranslationModel.locale == normalized, 0), else_=1))
        .limit(1)
        .correlate(MenuItemModel)
        .scalar_subquery()
    )
    fallback = (
        func.coalesce(MenuItemModel.name_ko, MenuItemModel.name_en)
        if normalized == "ko"
        else MenuItemModel.name_en
    )
    return func.coalesce(translated_name, fallback)


def _sql_search_matches(
    query: str,
    locale: str,
    *,
    include_enrichment: bool,
) -> tuple[Any, Any]:
    restaurant_values: tuple[Any, ...] = (
        RestaurantModel.name_en,
        RestaurantModel.name_ko,
    )
    if include_enrichment:
        restaurant_values += (
            RestaurantModel.description_en,
            RestaurantModel.description_ko,
            RestaurantModel.category,
        )
    restaurant_match = _sql_text_match(restaurant_values, query)
    base_values: tuple[Any, ...] = (
        *restaurant_values,
        MenuItemModel.name_en,
        MenuItemModel.name_ko,
    )
    if include_enrichment:
        base_values += (
            MenuCategoryModel.slug,
            MenuCategoryModel.name_en,
            MenuCategoryModel.name_ko,
            MenuItemModel.slug,
            MenuItemModel.description_en,
            MenuItemModel.description_ko,
        )
    base_match = _sql_text_match(
        base_values,
        query,
    )
    if not include_enrichment:
        return restaurant_match, base_match

    normalized_locale = normalize_locale(locale)
    language = normalized_locale.split("-", 1)[0]
    localized_restaurant_match = exists(
        select(1)
        .select_from(RestaurantTranslationModel)
        .where(
            RestaurantTranslationModel.restaurant_id == RestaurantModel.id,
            or_(
                RestaurantTranslationModel.locale == normalized_locale,
                RestaurantTranslationModel.locale == language,
                RestaurantTranslationModel.locale.like(f"{language}-%"),
            ),
            _sql_text_match(
                (
                    RestaurantTranslationModel.name,
                    RestaurantTranslationModel.description,
                ),
                query,
            ),
        )
        .correlate(RestaurantModel)
    )
    localized_category_match = exists(
        select(1)
        .select_from(MenuCategoryTranslationModel)
        .where(
            MenuCategoryTranslationModel.category_id == MenuCategoryModel.id,
            or_(
                MenuCategoryTranslationModel.locale == normalized_locale,
                MenuCategoryTranslationModel.locale == language,
                MenuCategoryTranslationModel.locale.like(f"{language}-%"),
            ),
            _sql_text_match((MenuCategoryTranslationModel.name,), query),
        )
        .correlate(MenuCategoryModel)
    )
    item_translation_match = exists(
        select(1)
        .select_from(MenuItemTranslationModel)
        .where(
            MenuItemTranslationModel.menu_item_id == MenuItemModel.id,
            _sql_text_match(
                (
                    MenuItemTranslationModel.name,
                    MenuItemTranslationModel.description,
                ),
                query,
            ),
        )
        .correlate(MenuItemModel)
    )
    ingredient_match = exists(
        select(1)
        .select_from(
            MenuItemIngredient.__table__.join(
                IngredientModel,
                IngredientModel.code == MenuItemIngredient.ingredient_code,
            )
        )
        .where(
            MenuItemIngredient.menu_item_id == MenuItemModel.id,
            _sql_text_match(
                (
                    MenuItemIngredient.ingredient_code,
                    IngredientModel.name_en,
                    IngredientModel.name_ko,
                ),
                query,
            ),
        )
        .correlate(MenuItemModel)
    )
    claim_match = exists(
        select(1)
        .select_from(MenuItemDietaryClaim)
        .where(
            MenuItemDietaryClaim.menu_item_id == MenuItemModel.id,
            _sql_text_match((MenuItemDietaryClaim.code,), query),
        )
        .correlate(MenuItemModel)
    )
    return restaurant_match, or_(
        base_match,
        localized_restaurant_match,
        localized_category_match,
        item_translation_match,
        ingredient_match,
        claim_match,
    )


def _sql_distance_meters(latitude: float, longitude: float) -> Any:
    latitude_delta = func.radians(RestaurantModel.latitude - latitude)
    longitude_delta = func.radians(RestaurantModel.longitude - longitude)
    haversine = func.power(func.sin(latitude_delta / 2), 2) + func.cos(
        func.radians(latitude)
    ) * func.cos(func.radians(RestaurantModel.latitude)) * func.power(
        func.sin(longitude_delta / 2), 2
    )
    clamped = case((haversine > 1, 1.0), else_=haversine)
    return cast(func.round(6_371_000.0 * 2 * func.asin(func.sqrt(clamped))), Integer)


def search_catalog(
    db: Session,
    *,
    query: str,
    locale: str,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_m: int | None = None,
    diet_codes: Sequence[str] = (),
    excluded_allergen_codes: Sequence[str] = (),
    ingredient_codes: Sequence[str] = (),
    main_ingredient_codes: Sequence[str] = (),
    dish_type_codes: Sequence[str] = (),
    price_codes: Sequence[str] = (),
    taste_codes: Sequence[str] = (),
    spicy: bool | None = None,
    solo_friendly: bool = False,
    max_price: int | None = None,
    taste: str | None = None,
    dish_type: str | None = None,
    open_now: bool = False,
    min_rating: float | None = None,
    cursor: str | None = None,
    limit: int = 20,
    user: UserModel | None = None,
) -> SearchResults:
    # Database text is stored in NFC (not the NFKD form used by Python-side matching).
    # Keeping SQL input in NFC is essential for Korean syllable searches.
    normalized_query = unicodedata.normalize("NFC", query.strip().casefold())
    diets = _codes(diet_codes)
    excluded = _codes(excluded_allergen_codes)
    ingredients = _codes(ingredient_codes)
    main_ingredients = _codes(main_ingredient_codes)
    dish_types = _codes(dish_type_codes)
    prices = _codes(price_codes)
    tastes = _codes(taste_codes)
    large_catalog = _catalog_is_large(db)
    if large_catalog and (latitude is None or longitude is None or radius_m is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "search_area_required",
                "message": "lat, lng, and radius_m are required for the production catalog.",
            },
        )
    if dish_type:
        dish_types.update(_codes((dish_type,)))
    if taste:
        tastes.update(_codes((taste,)))
    from_clause = MenuItemModel.__table__.join(
        MenuCategoryModel,
        MenuCategoryModel.id == MenuItemModel.category_id,
    ).join(
        RestaurantModel,
        RestaurantModel.id == MenuItemModel.restaurant_id,
    )
    predicates: list[Any] = [
        RestaurantModel.is_published.is_(True),
        MenuCategoryModel.is_active.is_(True),
        MenuItemModel.is_available.is_(True),
    ]
    predicates.extend(
        _sql_item_filter_predicates(
            diet_codes=diets,
            excluded_allergen_codes=excluded,
            ingredient_codes=ingredients,
            spicy=spicy,
            max_price=max_price,
            main_ingredient_codes=main_ingredients,
            dish_type_codes=dish_types,
            price_codes=prices,
            taste_codes=tastes,
            solo_friendly=solo_friendly,
            require_taste_evidence=large_catalog,
            require_allergen_evidence=large_catalog,
            treat_zero_spice_as_unknown=large_catalog,
            require_solo_evidence=large_catalog,
        )
    )

    distance_expression: Any = literal(0)
    if latitude is not None and longitude is not None:
        distance_expression = _sql_distance_meters(latitude, longitude)
        if radius_m is not None:
            latitude_delta = radius_m / 111_320
            longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.01)
            longitude_delta = radius_m / (111_320 * longitude_scale)
            predicates.extend(
                (
                    RestaurantModel.latitude.between(
                        latitude - latitude_delta,
                        latitude + latitude_delta,
                    ),
                    RestaurantModel.longitude.between(
                        longitude - longitude_delta,
                        longitude + longitude_delta,
                    ),
                    distance_expression <= radius_m,
                )
            )
    if open_now:
        predicates.append(
            _sql_open_now_predicate(
                db,
                datetime.now(timezone.utc),
                require_hours=large_catalog,
            )
        )
    if min_rating is not None:
        predicates.append(RestaurantModel.rating_avg >= min_rating)

    if normalized_query:
        restaurant_match, catalog_match = _sql_search_matches(
            normalized_query,
            locale,
            include_enrichment=not large_catalog,
        )
        predicates.append(catalog_match)
        localized_name = func.lower(
            func.coalesce(MenuItemModel.name_ko, MenuItemModel.name_en)
            if large_catalog and normalize_locale(locale) == "ko"
            else (MenuItemModel.name_en if large_catalog else _sql_localized_item_name(locale))
        )
        score_expression: Any = case(
            (localized_name == normalized_query, 12),
            (localized_name.startswith(normalized_query, autoescape=True), 9),
            (localized_name.contains(normalized_query, autoescape=True), 7),
            (restaurant_match, 4),
            else_=2,
        )
    else:
        score_expression = literal(0)

    offset = decode_cursor(cursor)
    if large_catalog and normalized_query:
        candidates = (
            select(
                MenuItemModel.id.label("item_id"),
                RestaurantModel.id.label("restaurant_id"),
                distance_expression.label("distance_m"),
                score_expression.label("score"),
                RestaurantModel.rating_avg.label("rating_avg"),
                RestaurantModel.slug.label("restaurant_slug"),
                MenuItemModel.sort_order.label("item_sort_order"),
            )
            .select_from(from_clause)
            .where(*predicates)
            .cte("search_candidates")
        )
        raw_page_rows = list(
            db.execute(
                select(
                    candidates.c.item_id,
                    candidates.c.restaurant_id,
                    candidates.c.distance_m,
                    select(func.count()).select_from(candidates).scalar_subquery(),
                    select(func.count(func.distinct(candidates.c.restaurant_id)))
                    .select_from(candidates)
                    .scalar_subquery(),
                )
                .order_by(
                    candidates.c.score.desc(),
                    candidates.c.distance_m,
                    candidates.c.rating_avg.desc(),
                    candidates.c.restaurant_slug,
                    candidates.c.item_sort_order,
                    candidates.c.item_id,
                )
                .offset(offset)
                .limit(limit)
            )
        )
        if raw_page_rows:
            item_count = int(raw_page_rows[0][3] or 0)
            restaurant_count = int(raw_page_rows[0][4] or 0)
        elif offset:
            item_count, restaurant_count = db.execute(
                select(
                    func.count(),
                    func.count(func.distinct(candidates.c.restaurant_id)),
                ).select_from(candidates)
            ).one()
            item_count = int(item_count or 0)
            restaurant_count = int(restaurant_count or 0)
        else:
            item_count = restaurant_count = 0
        page_rows = [(row[0], row[1], row[2]) for row in raw_page_rows]
    else:
        item_count, restaurant_count = db.execute(
            select(
                func.count(MenuItemModel.id),
                func.count(func.distinct(RestaurantModel.id)),
            )
            .select_from(from_clause)
            .where(*predicates)
        ).one()
        item_count = int(item_count or 0)
        restaurant_count = int(restaurant_count or 0)
        page_rows = list(
            db.execute(
                select(
                    MenuItemModel.id,
                    RestaurantModel.id,
                    distance_expression.label("distance_m"),
                )
                .select_from(from_clause)
                .where(*predicates)
                .order_by(
                    score_expression.desc(),
                    distance_expression,
                    RestaurantModel.rating_avg.desc(),
                    RestaurantModel.slug,
                    MenuItemModel.sort_order,
                    MenuItemModel.id,
                )
                .offset(offset)
                .limit(limit)
            )
        )
    next_offset = offset + len(page_rows)
    has_more = next_offset < item_count

    page_item_ids = [item_id for item_id, _, _ in page_rows]
    page_restaurant_ids = list(dict.fromkeys(restaurant_id for _, restaurant_id, _ in page_rows))
    items_by_id: dict[str, MenuItemModel] = {}
    restaurants_by_id: dict[str, RestaurantModel] = {}
    if page_item_ids:
        items_by_id = {
            item.id: item
            for item in db.scalars(
                select(MenuItemModel)
                .where(MenuItemModel.id.in_(page_item_ids))
                .options(*_item_options())
            ).unique()
        }
        restaurants_by_id = {
            restaurant.id: restaurant
            for restaurant in db.scalars(
                select(RestaurantModel)
                .where(RestaurantModel.id.in_(page_restaurant_ids))
                .options(
                    selectinload(RestaurantModel.translations),
                    selectinload(RestaurantModel.hours),
                )
            ).unique()
        }

    page = [
        (int(distance or 0), restaurants_by_id[restaurant_id], items_by_id[item_id])
        for item_id, restaurant_id, distance in page_rows
    ]
    # Restaurant summaries are intentionally derived from the bounded item page. The old
    # implementation returned every matching restaurant, which made a single search response
    # unbounded even though menu items themselves were paginated.
    restaurant_hits: dict[str, tuple[int, RestaurantModel, MenuItemModel]] = {}
    for distance, restaurant, item in page:
        restaurant_hits.setdefault(restaurant.id, (distance, restaurant, item))
    return SearchResults(
        query=query.strip(),
        items=[serialize_menu_item(item, locale, user) for _, _, item in page],
        restaurants=[
            serialize_restaurant(
                restaurant,
                locale,
                distance_m=distance or None,
                featured_item=item,
                user=user,
            )
            for distance, restaurant, item in restaurant_hits.values()
        ],
        item_count=item_count,
        restaurant_count=restaurant_count,
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
    )


def _section_title(key: str, locale: str) -> str:
    labels = {
        "en": {
            "main_ingredients": "Main ingredients",
            "dish_types": "Dish types",
            "dietary": "Dietary choices",
            "price": "Price",
            "taste": "Taste",
            "conditions": "Conditions",
        },
        "ko": {
            "main_ingredients": "주요 재료",
            "dish_types": "음식 종류",
            "dietary": "식단 선택",
            "price": "가격",
            "taste": "맛",
            "conditions": "조건",
        },
        "fr": {
            "main_ingredients": "Ingrédients principaux",
            "dish_types": "Types de plats",
            "dietary": "Régimes alimentaires",
            "price": "Prix",
            "taste": "Goût",
            "conditions": "Conditions",
        },
    }
    language = normalize_locale(locale).split("-", 1)[0]
    return labels.get(language, labels["en"])[key]


def search_facets(db: Session, *, locale: str) -> SearchFacets:
    large_catalog = _catalog_is_large(db)
    eligible_from = MenuItemModel.__table__.join(
        MenuCategoryModel,
        MenuCategoryModel.id == MenuItemModel.category_id,
    ).join(
        RestaurantModel,
        RestaurantModel.id == MenuItemModel.restaurant_id,
    )
    eligible = (
        RestaurantModel.is_published.is_(True),
        MenuCategoryModel.is_active.is_(True),
        MenuItemModel.is_available.is_(True),
    )
    aggregate_from = eligible_from
    aggregate_eligible = eligible

    ingredient_from = (
        MenuItemIngredient.__table__.join(
            IngredientModel,
            IngredientModel.code == MenuItemIngredient.ingredient_code,
        )
        .join(MenuItemModel, MenuItemModel.id == MenuItemIngredient.menu_item_id)
        .join(MenuCategoryModel, MenuCategoryModel.id == MenuItemModel.category_id)
        .join(RestaurantModel, RestaurantModel.id == MenuItemModel.restaurant_id)
    )
    ingredient_rows = list(
        db.execute(
            select(
                MenuItemIngredient.ingredient_code,
                IngredientModel.name_en,
                IngredientModel.name_ko,
                IngredientModel.emoji,
                func.count(func.distinct(MenuItemModel.id)),
            )
            .select_from(ingredient_from)
            .where(*eligible)
            .group_by(
                MenuItemIngredient.ingredient_code,
                IngredientModel.name_en,
                IngredientModel.name_ko,
                IngredientModel.emoji,
            )
        )
    )
    ingredient_items = {str(row[0]): int(row[4]) for row in ingredient_rows}
    ingredient_labels = {
        str(code): (localized(name_en, name_ko, locale), emoji)
        for code, name_en, name_ko, emoji, _ in ingredient_rows
    }
    main_count_columns = [
        func.count(
            func.distinct(
                case(
                    (
                        _sql_contains_any_alias(
                            (
                                MenuItemIngredient.ingredient_code,
                                IngredientModel.name_en,
                                IngredientModel.name_ko,
                            ),
                            aliases,
                        ),
                        MenuItemModel.id,
                    )
                )
            )
        )
        for aliases in _MAIN_INGREDIENT_GROUP_ALIASES.values()
    ]
    main_count_values = db.execute(
        select(*main_count_columns).select_from(ingredient_from).where(*eligible)
    ).one()
    main_group_counts = {
        code: int(main_count_values[index] or 0)
        for index, code in enumerate(_MAIN_INGREDIENT_GROUP_ALIASES)
    }

    category_items: dict[str, int] = defaultdict(int)
    category_labels: dict[str, str] = {}
    dish_group_counts = {code: 0 for code in _DISH_TYPE_GROUP_ALIASES}
    if not large_catalog:
        category_rows = list(
            db.execute(
                select(
                    MenuCategoryModel.slug,
                    func.min(MenuCategoryModel.id),
                    MenuCategoryModel.name_en,
                    MenuCategoryModel.name_ko,
                    func.count(MenuItemModel.id),
                )
                .select_from(eligible_from)
                .where(*eligible)
                .group_by(
                    MenuCategoryModel.slug,
                    MenuCategoryModel.name_en,
                    MenuCategoryModel.name_ko,
                )
            )
        )
        representative_category_ids = [str(row[1]) for row in category_rows]
        representative_categories = {
            category.id: category
            for category in db.scalars(
                select(MenuCategoryModel)
                .where(MenuCategoryModel.id.in_(representative_category_ids))
                .options(selectinload(MenuCategoryModel.translations))
            )
        }
        for slug, category_id, name_en, name_ko, count in category_rows:
            category = representative_categories[str(category_id)]
            category_items[str(slug)] += int(count)
            category_labels.setdefault(str(slug), _category_name(category, locale))
            category_values = (
                str(slug),
                str(name_en),
                str(name_ko or ""),
                *(translation.name for translation in category.translations),
            )
            for code, aliases in _DISH_TYPE_GROUP_ALIASES.items():
                if any(_contains_alias(value, aliases) for value in category_values):
                    dish_group_counts[code] += int(count)

    claim_from = (
        MenuItemDietaryClaim.__table__.join(
            MenuItemModel,
            MenuItemModel.id == MenuItemDietaryClaim.menu_item_id,
        )
        .join(MenuCategoryModel, MenuCategoryModel.id == MenuItemModel.category_id)
        .join(RestaurantModel, RestaurantModel.id == MenuItemModel.restaurant_id)
    )
    claim_items = {
        str(code): int(count)
        for code, count in db.execute(
            select(
                MenuItemDietaryClaim.code,
                func.count(func.distinct(MenuItemModel.id)),
            )
            .select_from(claim_from)
            .where(*eligible)
            .group_by(MenuItemDietaryClaim.code)
        )
    }

    dish_columns = (
        []
        if large_catalog
        else [
            func.sum(case((_sql_dish_type_predicate(code), 1), else_=0))
            for code in _DISH_TYPE_GROUP_ALIASES
        ]
    )
    price_columns = []
    for minimum, maximum_exclusive in _PRICE_BUCKETS.values():
        bucket = [MenuItemModel.price_amount.is_not(None)]
        if minimum is not None:
            bucket.append(MenuItemModel.price_amount >= minimum)
        if maximum_exclusive is not None:
            bucket.append(MenuItemModel.price_amount < maximum_exclusive)
        price_columns.append(func.sum(case((and_(*bucket), 1), else_=0)))
    if large_catalog:
        core_counts = db.execute(
            select(*price_columns).select_from(aggregate_from).where(*aggregate_eligible)
        ).one()
        prices = {code: int(core_counts[index] or 0) for index, code in enumerate(_PRICE_BUCKETS)}
        curated_taste_counts = {code: 0 for code in _CURATED_TASTE_CODES}
        solo_restaurant_count = 0
    else:
        core_counts = db.execute(
            select(
                *dish_columns,
                *price_columns,
                func.sum(case((MenuItemModel.spice_level == 0, 1), else_=0)),
                func.sum(case((MenuItemModel.spice_level <= 1, 1), else_=0)),
                func.count(
                    func.distinct(case((_sql_solo_friendly_predicate(), RestaurantModel.id)))
                ),
            )
            .select_from(aggregate_from)
            .where(*aggregate_eligible)
        ).one()
        dish_group_counts = {
            code: int(core_counts[index] or 0)
            for index, code in enumerate(_DISH_TYPE_GROUP_ALIASES)
        }
        price_start = len(dish_columns)
        prices = {
            code: int(core_counts[price_start + index] or 0)
            for index, code in enumerate(_PRICE_BUCKETS)
        }
        taste_start = price_start + len(_PRICE_BUCKETS)
        curated_taste_counts = {
            "not-spicy": int(core_counts[taste_start] or 0),
            "mild": int(core_counts[taste_start + 1] or 0),
        }
        solo_restaurant_count = int(core_counts[taste_start + 2] or 0)

    taste_items: dict[str, int] = defaultdict(int)
    if not large_catalog:
        profile_expression = cast(MenuItemModel.taste_profile, Text)
        for raw_profile, count in db.execute(
            select(profile_expression, func.count(MenuItemModel.id))
            .select_from(eligible_from)
            .where(*eligible)
            .group_by(profile_expression)
        ):
            if not raw_profile:
                continue
            try:
                profile = json.loads(raw_profile)
            except (TypeError, ValueError):
                continue
            if not isinstance(profile, dict):
                continue
            for raw_code, raw_strength in profile.items():
                if not isinstance(raw_strength, (int, float)) or raw_strength < 0.5:
                    continue
                code = str(raw_code)
                taste_items[code] += int(count)
                normalized_code = _fold(code).replace("_", "-").replace(" ", "-")
                if normalized_code in {"rich", "light", "crispy"}:
                    curated_taste_counts[normalized_code] = curated_taste_counts.get(
                        normalized_code, 0
                    ) + int(count)
    for code in ("rich", "light", "crispy"):
        curated_taste_counts.setdefault(code, 0)

    if large_catalog:
        open_restaurant_count = 0
    else:
        now = datetime.now(timezone.utc)
        open_restaurant_count = int(
            db.scalar(
                select(func.count(RestaurantModel.id)).where(
                    RestaurantModel.is_published.is_(True),
                    _sql_open_now_predicate(db, now),
                )
            )
            or 0
        )
    rating_restaurant_count = int(
        db.scalar(
            select(func.count(RestaurantModel.id)).where(
                RestaurantModel.is_published.is_(True),
                RestaurantModel.rating_avg >= 4.3,
            )
        )
        or 0
    )

    main_ingredient_labels = {
        "seafood": ("Seafood", "🦐"),
        "beef": ("Beef", "🥩"),
        "chicken": ("Chicken", "🍗"),
        "vegetables": ("Vegetables", "🥬"),
    }
    curated_ingredient_options = [
        SearchFacetOption(
            code=code,
            label=main_ingredient_labels[code][0],
            count=main_group_counts[code],
            metadata={
                "emoji": main_ingredient_labels[code][1],
                "entity": "menu_item",
                "selection": "or",
                "curated": True,
            },
        )
        for code in main_ingredient_labels
    ]
    extra_ingredient_options = [
        SearchFacetOption(
            code=code,
            label=ingredient_labels[code][0],
            count=count,
            metadata={
                "emoji": ingredient_labels[code][1],
                "entity": "menu_item",
                "selection": "or",
            },
        )
        for code, count in sorted(ingredient_items.items(), key=lambda pair: (-pair[1], pair[0]))
        if code not in main_ingredient_labels
    ]
    ingredient_options = curated_ingredient_options + extra_ingredient_options

    dish_group_labels = {
        "bbq-grilled": "BBQ & grilled",
        "soup-stew": "Soup & stew",
        "noodles": "Noodles",
        "rice-dishes": "Rice dishes",
    }
    curated_category_options = [
        SearchFacetOption(
            code=code,
            label=label,
            count=dish_group_counts[code],
            metadata={
                "entity": "menu_item",
                "selection": "or",
                "curated": True,
                **(
                    {
                        "supported": False,
                        "unavailable_reason": "dish_taxonomy_unavailable",
                    }
                    if large_catalog
                    else {}
                ),
            },
        )
        for code, label in dish_group_labels.items()
    ]
    extra_category_options = [
        SearchFacetOption(
            code=code,
            label=category_labels[code],
            count=count,
            metadata={"entity": "menu_item", "selection": "or"},
        )
        for code, count in sorted(category_items.items(), key=lambda pair: (-pair[1], pair[0]))
        if code not in dish_group_labels
    ]
    category_options = curated_category_options + extra_category_options
    dietary_options = [
        SearchFacetOption(
            code=code,
            label=code.replace("-", " ").title(),
            count=count,
        )
        for code, count in sorted(claim_items.items())
    ]
    price_labels = {
        "under-10000": "Under ₩10k",
        "10000-20000": "₩10k–20k",
        "20000-35000": "₩20k–35k",
        "35000-plus": "₩35k+",
    }
    price_metadata = {
        "under-10000": {"max_exclusive": 10_000},
        "10000-20000": {"min": 10_000, "max_exclusive": 20_000},
        "20000-35000": {"min": 20_000, "max_exclusive": 35_000},
        "35000-plus": {"min": 35_000},
    }
    price_options = [
        SearchFacetOption(
            code=code,
            label=price_labels[code],
            count=count,
            metadata={
                **price_metadata[code],
                "entity": "menu_item",
                "selection": "or",
                "curated": True,
            },
        )
        for code, count in prices.items()
    ]
    curated_taste_labels = {
        "not-spicy": "Not spicy",
        "mild": "Mild",
        "rich": "Rich",
        "light": "Light",
        "crispy": "Crispy",
    }
    curated_taste_options = [
        SearchFacetOption(
            code=code,
            label=label,
            count=curated_taste_counts[code],
            metadata={
                "entity": "menu_item",
                "selection": "or",
                "curated": True,
                **(
                    {
                        "supported": False,
                        "unavailable_reason": "taste_profile_unavailable",
                    }
                    if large_catalog
                    else {}
                ),
            },
        )
        for code, label in curated_taste_labels.items()
    ]
    extra_taste_options = [
        SearchFacetOption(
            code=code,
            label=code.replace("-", " ").title(),
            count=count,
            metadata={"entity": "menu_item", "selection": "or"},
        )
        for code, count in sorted(taste_items.items(), key=lambda pair: (-pair[1], pair[0]))
        if code not in curated_taste_labels
    ]
    taste_options = curated_taste_options + extra_taste_options
    conditions = [
        SearchFacetOption(
            code="open-now",
            label="Open now",
            count=open_restaurant_count,
            metadata={
                "entity": "restaurant",
                **(
                    {
                        "supported": False,
                        "unavailable_reason": "opening_hours_unavailable",
                    }
                    if large_catalog
                    else {}
                ),
            },
        ),
        SearchFacetOption(
            code="10-min-walk",
            label="10 min walk",
            count=0,
            metadata={
                "entity": "restaurant",
                "radius_m": 800,
                "count_requires_location": True,
            },
        ),
        SearchFacetOption(
            code="solo-friendly",
            label="Solo-friendly",
            count=solo_restaurant_count,
            metadata={
                "entity": "restaurant",
                **(
                    {
                        "supported": False,
                        "unavailable_reason": "serving_size_unavailable",
                    }
                    if large_catalog
                    else {}
                ),
            },
        ),
        SearchFacetOption(
            code="rating-4.3-plus",
            label="4.3+ rating",
            count=rating_restaurant_count,
            metadata={"entity": "restaurant", "minimum": 4.3},
        ),
    ]
    sections = [
        ("main_ingredients", ingredient_options),
        ("dish_types", category_options),
        ("dietary", dietary_options),
        ("price", price_options),
        ("taste", taste_options),
        ("conditions", conditions),
    ]
    return SearchFacets(
        sections=[
            SearchFacetSection(
                key=key,
                title=_section_title(key, locale),
                options=options,
            )
            for key, options in sections
        ]
    )


def trending_searches(db: Session, *, locale: str) -> TrendingSearches:
    curated = [
        ("tteokbokki", 2_400, "Tteokbokki"),
        ("samgyeopsal", 1_900, "Korean BBQ"),
        ("vegan-bibimbap", 1_200, "Vegan Bibimbap"),
    ]
    curated_slugs = [slug for slug, _, _ in curated]
    selected_items = (
        select(
            MenuItemModel.slug.label("slug"),
            func.min(MenuItemModel.id).label("item_id"),
        )
        .join(RestaurantModel, RestaurantModel.id == MenuItemModel.restaurant_id)
        .where(
            MenuItemModel.slug.in_(curated_slugs),
            MenuItemModel.is_available.is_(True),
            RestaurantModel.is_published.is_(True),
        )
        .group_by(MenuItemModel.slug)
        .subquery()
    )
    statement = (
        select(MenuItemModel)
        .join(selected_items, selected_items.c.item_id == MenuItemModel.id)
        .options(selectinload(MenuItemModel.translations))
    )
    items_by_slug = {item.slug: item for item in db.scalars(statement)}

    items: list[TrendingSearch] = []
    for rank, (slug, search_count, english_query) in enumerate(curated, start=1):
        item = items_by_slug.get(slug)
        if item is None:
            continue
        localized_name, _, _ = _item_text(item, locale)
        query = english_query if normalize_locale(locale) == "en" else localized_name
        items.append(
            TrendingSearch(
                rank=rank,
                query=query,
                subtitle=f"{search_count / 1000:.1f}k searches",
                search_count=search_count,
                menu_item_id=item.id,
            )
        )
    return TrendingSearches(items=items)


def explore_feed(
    db: Session,
    *,
    category: str | None,
    cursor: str | None,
    limit: int,
) -> ExplorePage:
    statement = (
        select(ExploreVideoModel)
        .where(ExploreVideoModel.is_published.is_(True))
        .order_by(ExploreVideoModel.created_at.desc(), ExploreVideoModel.id)
    )
    videos = list(db.scalars(statement))
    normalized_category = _fold(category).replace(" ", "-")
    if normalized_category:
        videos = [
            video
            for video in videos
            if normalized_category
            in {_fold(value).replace(" ", "-") for value in (video.categories or [])}
        ]
    offset = decode_cursor(cursor)
    page = videos[offset : offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < len(videos)
    return ExplorePage(
        items=[
            ExploreVideo(
                id=video.id,
                provider=video.provider,
                provider_video_id=video.provider_video_id,
                title=video.title,
                creator=video.creator,
                thumbnail_url=video.thumbnail_url,
                playback_url=(
                    f"https://www.youtube.com/shorts/{video.provider_video_id}"
                    if video.provider == "youtube"
                    else video.provider_video_id
                ),
                categories=video.categories or [],
            )
            for video in page
        ],
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
        total=len(videos),
    )
