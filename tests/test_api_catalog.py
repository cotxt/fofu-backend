from __future__ import annotations

import pytest
from conftest import API_V1, DEMO_PASSWORD
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from app import models
from app.database import SessionLocal
from app.services import catalog as catalog_service


def _search_items(client: TestClient, **params: object) -> dict[str, dict[str, object]]:
    response = client.get(
        f"{API_V1}/search",
        params={"limit": 50, **params},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_more"] is False
    return {item["slug"]: item for item in body["items"]}


def _restaurant_page(client: TestClient, **params: object) -> dict[str, object]:
    response = client.get(
        f"{API_V1}/restaurants",
        params={"limit": 100, **params},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_more"] is False
    return body


def _restaurant_slugs(client: TestClient, **params: object) -> set[str]:
    page = _restaurant_page(client, **params)
    return {item["slug"] for item in page["items"]}


def _demo_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": "demo@fofu.app",
            "password": DEMO_PASSWORD,
            "client_type": "ios",
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_catalog_is_localized_paginated_and_database_backed(client: TestClient) -> None:
    restaurants = client.get(f"{API_V1}/restaurants", params={"locale": "fr", "limit": 2})
    assert restaurants.status_code == 200
    page = restaurants.json()
    assert len(page["items"]) == 2
    assert page["total"] == 4
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert page["items"][0]["name"] == "La Table de Halmoni"
    assert isinstance(page["items"][0]["distance_m"], int)
    assert isinstance(page["items"][0]["featured_item"]["price"]["amount"], int)

    second = client.get(
        f"{API_V1}/restaurants",
        params={"locale": "fr", "limit": 2, "cursor": page["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 2
    assert second.json()["has_more"] is False

    facets = client.get(f"{API_V1}/search/facets", params={"locale": "en"})
    trending = client.get(f"{API_V1}/search/trending", params={"locale": "en"})
    explore = client.get(f"{API_V1}/explore", params={"limit": 5})
    assert facets.status_code == trending.status_code == explore.status_code == 200
    assert facets.json()["sections"]
    assert trending.json()["items"]
    assert len(explore.json()["items"]) == 5
    assert explore.json()["has_more"] is True


def test_nearby_page_size_is_capped_at_one_hundred(client: TestClient) -> None:
    accepted = client.get(f"{API_V1}/restaurants", params={"limit": 100})
    rejected = client.get(f"{API_V1}/restaurants", params={"limit": 101})

    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_food_passport_personalizes_compatibility_without_safety_guarantees(
    client: TestClient,
) -> None:
    anonymous = client.get(f"{API_V1}/menu-items/samgyeopsal", params={"locale": "en"})
    assert anonymous.status_code == 200
    assert anonymous.json()["compatibility"]["status"] == "unknown"
    assert "not a medical guarantee" in anonymous.json()["compatibility"]["disclaimer"]

    personalized = client.get(
        f"{API_V1}/menu-items/samgyeopsal",
        params={"locale": "fr"},
        headers=_demo_headers(client),
    )
    assert personalized.status_code == 200
    item = personalized.json()
    assert item["name"] == "Poitrine de porc grillée"
    assert item["compatibility"]["status"] == "conflict"
    conflicts = item["compatibility"]["matched_conflicts"]
    assert any(conflict["code"] == "pork" for conflict in conflicts)
    assert "confirm severe allergies" in item["compatibility"]["disclaimer"]

    compatible = client.get(f"{API_V1}/menu-items/japchae", headers=_demo_headers(client))
    assert compatible.status_code == 200
    assert compatible.json()["compatibility"]["status"] == "compatible"

    with SessionLocal() as db:
        japchae = db.scalar(select(models.MenuItem).where(models.MenuItem.slug == "japchae"))
        assert japchae is not None
        claim = db.get(models.MenuItemDietaryClaim, (japchae.id, "vegetarian"))
        assert claim is not None
        claim.verification_status = "unverified"
        db.commit()
    try:
        unverified = client.get(f"{API_V1}/menu-items/japchae", headers=_demo_headers(client))
        assert unverified.status_code == 200
        assert unverified.json()["compatibility"]["status"] == "unknown"
        assert "diet:pescatarian" in unverified.json()["compatibility"]["missing_evidence"]
    finally:
        with SessionLocal() as db:
            claim = db.get(models.MenuItemDietaryClaim, (japchae.id, "vegetarian"))
            assert claim is not None
            claim.verification_status = "merchant_reported"
            db.commit()


def test_search_validates_coordinates_and_returns_real_matches(client: TestClient) -> None:
    invalid = client.get(f"{API_V1}/search", params={"q": "kimchi", "lat": 37.55})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "incomplete_coordinates"

    result = client.get(
        f"{API_V1}/search",
        params={"q": "kimchi", "lat": 37.5563, "lng": 126.9236, "radius_m": 5000},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["query"] == "kimchi"
    assert body["item_count"] >= 1
    assert any(item["slug"] == "kimchi-jjigae" for item in body["items"])

    pescatarian = client.get(f"{API_V1}/search", params={"q": "japchae", "diet": "pescatarian"})
    assert pescatarian.status_code == 200
    assert any(item["slug"] == "japchae" for item in pescatarian.json()["items"])


def test_curated_multi_select_is_or_within_each_facet_on_search_and_restaurants(
    client: TestClient,
) -> None:
    cases = [
        ("main_ingredient", "seafood", "beef"),
        ("dish_type", "bbq-grilled", "soup-stew"),
        ("price", "under-10000", "10000-20000"),
        ("taste", "not-spicy", "rich"),
    ]

    for parameter, first_value, second_value in cases:
        first_items = set(_search_items(client, **{parameter: first_value}))
        second_items = set(_search_items(client, **{parameter: second_value}))
        combined_items = set(_search_items(client, **{parameter: f"{first_value},{second_value}"}))
        assert first_items or second_items
        assert combined_items == first_items | second_items

        first_restaurants = _restaurant_slugs(client, **{parameter: first_value})
        second_restaurants = _restaurant_slugs(client, **{parameter: second_value})
        combined_restaurants = _restaurant_slugs(
            client, **{parameter: f"{first_value},{second_value}"}
        )
        assert combined_restaurants == first_restaurants | second_restaurants


def test_curated_facets_are_and_across_facets_on_search_and_restaurants(
    client: TestClient,
) -> None:
    filters: dict[str, object] = {
        "main_ingredient": "seafood",
        "dish_type": "bbq-grilled",
        "price": "10000-20000",
        "taste": "not-spicy",
        "solo_friendly": True,
    }
    single_facet_results = [
        set(_search_items(client, **{parameter: value})) for parameter, value in filters.items()
    ]
    expected_items = set.intersection(*single_facet_results)
    combined_items = set(_search_items(client, **filters))

    assert expected_items == {"grilled-mackerel"}
    assert combined_items == expected_items

    page = _restaurant_page(client, **filters)
    assert {item["slug"] for item in page["items"]} == {"ocean-table"}
    assert {item["featured_item"]["slug"] for item in page["items"]} == expected_items


def test_price_buckets_use_exact_exclusive_boundaries_on_search(client: TestClient) -> None:
    boundary_prices = {
        "samgyeopsal": 9_999,
        "kimchi-jjigae": 10_000,
        "japchae": 19_999,
        "vegan-bibimbap": 20_000,
        "tteokbokki": 34_999,
        "haemul-pajeon": 35_000,
    }
    with SessionLocal() as db:
        items = list(
            db.scalars(select(models.MenuItem).where(models.MenuItem.slug.in_(boundary_prices)))
        )
        assert {item.slug for item in items} == set(boundary_prices)
        original_prices = {item.slug: item.price_amount for item in items}
        for item in items:
            item.price_amount = boundary_prices[item.slug]
        db.commit()

    try:
        boundary_slugs = set(boundary_prices)
        expected_by_bucket = {
            "under-10000": {"samgyeopsal"},
            "10000-20000": {"kimchi-jjigae", "japchae"},
            "20000-35000": {"vegan-bibimbap", "tteokbokki"},
            "35000-plus": {"haemul-pajeon"},
        }
        seen: set[str] = set()
        for bucket, expected in expected_by_bucket.items():
            matches = set(_search_items(client, price=bucket)) & boundary_slugs
            assert matches == expected
            assert not seen.intersection(matches)
            seen.update(matches)
        assert seen == boundary_slugs
    finally:
        with SessionLocal() as db:
            items = list(
                db.scalars(select(models.MenuItem).where(models.MenuItem.slug.in_(original_prices)))
            )
            for item in items:
                item.price_amount = original_prices[item.slug]
            db.commit()


def test_unknown_price_is_visible_but_excluded_from_price_filters(
    client: TestClient,
) -> None:
    with SessionLocal() as db:
        item = db.scalar(select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal"))
        assert item is not None
        original_price = item.price_amount
        item.price_amount = None
        db.commit()

    try:
        detail = client.get(f"{API_V1}/menu-items/samgyeopsal")
        assert detail.status_code == 200
        body = detail.json()
        assert "price" not in body
        assert body["is_available"] is True
        assert body["is_orderable"] is True
        assert body["orderability_reason"] == "price_unknown"

        assert "samgyeopsal" in _search_items(client)
        assert "samgyeopsal" not in _search_items(client, max_price=500_000)
        assert "samgyeopsal" not in _search_items(client, price="under-10000")

        facets = client.get(f"{API_V1}/search/facets")
        assert facets.status_code == 200, facets.text
    finally:
        with SessionLocal() as db:
            item = db.scalar(select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal"))
            assert item is not None
            item.price_amount = original_price
            db.commit()


def test_solo_friendly_is_deterministic_for_search_and_restaurants(
    client: TestClient,
) -> None:
    expected_solo_items = {
        "samgyeopsal",
        "kimchi-jjigae",
        "vegan-bibimbap",
        "tteokbokki",
        "mushroom-bulgogi",
        "perilla-noodles",
        "cheese-tteokbokki",
        "grilled-mackerel",
        "abalone-porridge",
    }
    first = _search_items(client, solo_friendly=True)
    second = _search_items(client, solo_friendly=True)
    assert set(first) == set(second) == expected_solo_items
    assert all(item["serving_description"].startswith("One ") for item in first.values())

    first_page = _restaurant_page(client, solo_friendly=True)
    second_page = _restaurant_page(client, solo_friendly=True)
    assert [item["slug"] for item in first_page["items"]] == [
        item["slug"] for item in second_page["items"]
    ]
    assert {item["slug"] for item in first_page["items"]} == {
        "halmonis-table",
        "green-bowl",
        "seoul-spice",
        "ocean-table",
    }
    assert all(item["featured_item"]["slug"] in expected_solo_items for item in first_page["items"])


def test_search_facets_are_curated_ordered_and_counted(client: TestClient) -> None:
    response = client.get(f"{API_V1}/search/facets", params={"locale": "en"})
    assert response.status_code == 200
    sections = response.json()["sections"]
    section_keys = [section["key"] for section in sections]
    assert section_keys[:2] == ["main_ingredients", "dish_types"]
    assert {"main_ingredients", "dish_types", "dietary", "price", "taste", "conditions"} <= set(
        section_keys
    )
    assert (
        section_keys.index("price") < section_keys.index("taste") < section_keys.index("conditions")
    )

    by_key = {section["key"]: section for section in sections}
    expected_options = {
        "main_ingredients": [
            ("seafood", "Seafood"),
            ("beef", "Beef"),
            ("chicken", "Chicken"),
            ("vegetables", "Vegetables"),
        ],
        "dish_types": [
            ("bbq-grilled", "BBQ & grilled"),
            ("soup-stew", "Soup & stew"),
            ("noodles", "Noodles"),
            ("rice-dishes", "Rice dishes"),
        ],
        "price": [
            ("under-10000", "Under ₩10k"),
            ("10000-20000", "₩10k–20k"),
            ("20000-35000", "₩20k–35k"),
            ("35000-plus", "₩35k+"),
        ],
        "taste": [
            ("not-spicy", "Not spicy"),
            ("mild", "Mild"),
            ("rich", "Rich"),
            ("light", "Light"),
            ("crispy", "Crispy"),
        ],
        "conditions": [
            ("open-now", "Open now"),
            ("10-min-walk", "10 min walk"),
            ("solo-friendly", "Solo-friendly"),
            ("rating-4.3-plus", "4.3+ rating"),
        ],
    }
    for key, expected in expected_options.items():
        actual = [(option["code"], option["label"]) for option in by_key[key]["options"]]
        if key in {"main_ingredients", "dish_types", "taste"}:
            assert actual[: len(expected)] == expected
        else:
            assert actual == expected

    expected_curated_counts = {
        "main_ingredients": [5, 0, 0, 10],
        "dish_types": [2, 1, 2, 3],
        "price": [2, 10, 0, 0],
        "taste": [9, 9, 4, 5, 3],
        "conditions": [
            _restaurant_page(client, open_now=True)["total"],
            0,
            4,
            4,
        ],
    }
    for key, expected_counts in expected_curated_counts.items():
        assert [
            option["count"] for option in by_key[key]["options"][: len(expected_counts)]
        ] == expected_counts

    for key, parameter in (
        ("main_ingredients", "main_ingredient"),
        ("dish_types", "dish_type"),
        ("price", "price"),
        ("taste", "taste"),
    ):
        for option in by_key[key]["options"]:
            result = _search_items(client, **{parameter: option["code"]})
            assert option["count"] == len(result)

    conditions = {option["code"]: option for option in by_key["conditions"]["options"]}
    assert conditions["10-min-walk"]["count"] == 0
    assert conditions["10-min-walk"]["metadata"] == {
        "radius_m": 800,
        "count_requires_location": True,
        "entity": "restaurant",
    }
    assert conditions["solo-friendly"]["count"] == 4
    assert conditions["rating-4.3-plus"]["count"] == 4


def test_search_and_facets_never_load_the_full_catalog_graph(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_full_catalog_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("search endpoints must aggregate and page in SQL")

    monkeypatch.setattr(catalog_service, "_load_restaurants", fail_full_catalog_load)

    search = client.get(
        f"{API_V1}/search",
        params={
            "q": "kimchi",
            "lat": 37.5563,
            "lng": 126.9236,
            "radius_m": 5_000,
            "limit": 1,
        },
    )
    assert search.status_code == 200, search.text
    body = search.json()
    assert len(body["items"]) == 1
    assert len(body["restaurants"]) <= 1
    assert body["item_count"] >= 1

    facets = client.get(f"{API_V1}/search/facets", params={"locale": "en"})
    assert facets.status_code == 200, facets.text
    assert facets.json()["sections"]

    nearby = client.get(
        f"{API_V1}/restaurants",
        params={"price": "10000-20000", "limit": 1},
    )
    assert nearby.status_code == 200, nearby.text
    assert len(nearby.json()["items"]) == 1


def test_large_catalog_fast_path_is_bounded_and_marks_missing_facets_unsupported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_service, "_catalog_is_large", lambda _db: True)

    def fail_full_catalog_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("large-catalog endpoints must not hydrate the full graph")

    monkeypatch.setattr(catalog_service, "_load_restaurants", fail_full_catalog_load)

    search = client.get(
        f"{API_V1}/search",
        params={
            "q": "kimchi",
            "lat": 37.5563,
            "lng": 126.9236,
            "radius_m": 5_000,
            "limit": 1,
        },
    )
    assert search.status_code == 200, search.text
    assert len(search.json()["items"]) == 1

    unbounded_search = client.get(
        f"{API_V1}/search",
        params={"q": "kimchi", "limit": 1},
    )
    assert unbounded_search.status_code == 422
    assert unbounded_search.json()["error"]["code"] == "search_area_required"

    facets = client.get(f"{API_V1}/search/facets", params={"locale": "en"})
    assert facets.status_code == 200, facets.text
    sections = {section["key"]: section for section in facets.json()["sections"]}
    for key in ("dish_types", "taste"):
        assert all(option["count"] == 0 for option in sections[key]["options"])
        assert all(option["metadata"]["supported"] is False for option in sections[key]["options"])
    conditions = {option["code"]: option for option in sections["conditions"]["options"]}
    assert conditions["open-now"]["metadata"]["supported"] is False
    assert conditions["solo-friendly"]["metadata"]["supported"] is False

    unsupported_taste = client.get(
        f"{API_V1}/restaurants",
        params={"taste": "mild", "limit": 1},
    )
    assert unsupported_taste.status_code == 200, unsupported_taste.text
    assert unsupported_taste.json()["total"] == 0

    for unsupported_filter in (
        {"spicy": "false"},
        {"solo_friendly": "true"},
        {"exclude_allergen": "pork"},
    ):
        response = client.get(
            f"{API_V1}/restaurants",
            params={**unsupported_filter, "limit": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 0


def test_unfiltered_nearby_pages_before_loading_catalog_relationships(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    restaurant_ids = [f"nearby-two-phase-{index}" for index in range(101)]
    latitude = 35.0
    longitude = 127.0
    with SessionLocal() as db:
        for index, restaurant_id in enumerate(restaurant_ids):
            category_id = f"nearby-category-{index}"
            db.add(
                models.Restaurant(
                    id=restaurant_id,
                    slug=restaurant_id,
                    owner_user_id=None,
                    name_en=f"Two-phase Restaurant {index}",
                    name_ko=f"2단계 식당 {index}",
                    description_en="Dense nearby-query regression fixture.",
                    description_ko="조밀한 주변 조회 회귀 테스트 데이터입니다.",
                    handle=f"@{restaurant_id}",
                    category="Regression fixture",
                    hero_style="charcoal",
                    address_en=f"Test address {index}",
                    address_ko=f"테스트 주소 {index}",
                    phone=None,
                    latitude=latitude + index * 0.0001,
                    longitude=longitude,
                    currency="KRW",
                    timezone_name="Asia/Seoul",
                    rating_avg=4.5,
                    rating_count=index,
                    is_verified=False,
                    is_open=True,
                    is_published=True,
                    menu_revision=1,
                    cover_image_url=None,
                    gallery=[],
                )
            )
            db.add(
                models.MenuCategory(
                    id=category_id,
                    restaurant_id=restaurant_id,
                    slug="menu",
                    name_en="Menu",
                    name_ko="메뉴",
                    sort_order=0,
                    is_active=True,
                )
            )
            db.add(
                models.MenuItem(
                    id=f"nearby-item-{index}",
                    restaurant_id=restaurant_id,
                    category_id=category_id,
                    slug=f"featured-{index}",
                    name_en=f"Featured dish {index}",
                    name_ko=f"대표 메뉴 {index}",
                    pronunciation=None,
                    description_en="Featured fixture item.",
                    description_ko="대표 테스트 메뉴입니다.",
                    price_amount=10_000 + index,
                    currency="KRW",
                    serving_description=None,
                    spice_level=0,
                    taste_profile={},
                    local_tips=[],
                    badge=None,
                    image_url=None,
                    media=[],
                    is_available=True,
                    sort_order=0,
                )
            )
        db.commit()

    def reject_legacy_load(*_args: object, **_kwargs: object) -> list[models.Restaurant]:
        raise AssertionError("unfiltered nearby queries must not load every restaurant graph")

    monkeypatch.setattr(catalog_service, "_load_restaurants", reject_legacy_load)
    loaded_menu_restaurant_ids: set[str] = set()

    def track_loaded_menu_item(item: models.MenuItem, _context: object) -> None:
        loaded_menu_restaurant_ids.add(item.restaurant_id)

    event.listen(models.MenuItem, "load", track_loaded_menu_item)
    try:
        first = client.get(
            f"{API_V1}/restaurants",
            params={"lat": latitude, "lng": longitude, "radius_m": 5_000, "limit": 100},
        )
        assert first.status_code == 200, first.text
        first_page = first.json()
        first_ids = [item["id"] for item in first_page["items"]]
        assert first_ids == restaurant_ids[:100]
        assert first_page["total"] == len(restaurant_ids)
        assert first_page["has_more"] is True
        assert first_page["next_cursor"]
        assert first_page["items"][0]["featured_item"]["slug"] == "featured-0"
        assert loaded_menu_restaurant_ids == set(first_ids)

        loaded_menu_restaurant_ids.clear()
        second = client.get(
            f"{API_V1}/restaurants",
            params={
                "lat": latitude,
                "lng": longitude,
                "radius_m": 5_000,
                "limit": 100,
                "cursor": first_page["next_cursor"],
            },
        )
        assert second.status_code == 200, second.text
        second_page = second.json()
        second_ids = [item["id"] for item in second_page["items"]]
        assert second_ids == restaurant_ids[100:]
        assert second_page["total"] == len(restaurant_ids)
        assert second_page["has_more"] is False
        assert "next_cursor" not in second_page
        assert second_page["items"][0]["featured_item"]["slug"] == "featured-100"
        assert loaded_menu_restaurant_ids == set(second_ids)
    finally:
        event.remove(models.MenuItem, "load", track_loaded_menu_item)
        with SessionLocal() as db:
            db.execute(delete(models.Restaurant).where(models.Restaurant.id.in_(restaurant_ids)))
            db.commit()
