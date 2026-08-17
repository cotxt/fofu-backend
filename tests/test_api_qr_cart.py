from __future__ import annotations

import uuid

from conftest import API_V1, DEMO_PASSWORD, DEMO_QR_CODE
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.database import SessionLocal


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _find_item(bootstrap: dict[str, object], slug: str) -> dict[str, object]:
    categories = bootstrap["menu"]
    assert isinstance(categories, list)
    return next(
        item
        for category in categories
        for item in category["items"]
        if item["slug"] == slug
    )


def test_qr_redirect_exchange_and_bootstrap_are_scoped(client: TestClient) -> None:
    invalid = client.get(f"{API_V1}/qr/not-a-real-code-000", follow_redirects=False)
    assert invalid.status_code == 404
    assert invalid.json()["error"]["code"] == "qr_not_found"

    redirect = client.get(f"/q/{DEMO_QR_CODE}", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == (
        f"http://web.test/r/halmonis-table#qr={DEMO_QR_CODE}"
    )
    assert redirect.headers["cache-control"] == "no-store"
    assert redirect.headers["referrer-policy"] == "no-referrer"

    exchange = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={
            "code": DEMO_QR_CODE,
            "locale": "fr",
            "client_type": "ios",
            "device_id": "qr-integration-device",
        },
    )
    assert exchange.status_code == 201
    body = exchange.json()
    assert body["scope"] == "qr_guest"
    assert body["refresh_token"]
    bootstrap = body["bootstrap"]
    assert bootstrap["session"]["scope"] == "qr_guest"
    assert bootstrap["session"]["restaurant_id"] == bootstrap["restaurant"]["id"]
    assert bootstrap["restaurant"]["slug"] == "halmonis-table"
    assert bootstrap["restaurant"]["name"] == "La Table de Halmoni"
    assert _find_item(bootstrap, "samgyeopsal")["name"] == "Poitrine de porc grillée"

    reloaded = client.get(
        f"{API_V1}/sessions/current/bootstrap",
        headers=_bearer(body["access_token"]),
        params={"locale": "ko"},
    )
    assert reloaded.status_code == 200
    assert reloaded.json()["restaurant"]["name"] == "할머니 식탁"


def test_consecutive_qr_exchange_replaces_only_the_current_qr_session(
    client: TestClient,
) -> None:
    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": f"qr-replacement-{uuid.uuid4().hex}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "QR Replacement",
            "client_type": "ios",
            "device_id": "full-backup",
        },
    )
    assert registered.status_code == 201
    full = registered.json()

    first_qr = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=_bearer(full["access_token"]),
        json={
            "code": DEMO_QR_CODE,
            "locale": "en",
            "client_type": "ios",
            "device_id": "first-qr",
        },
    )
    assert first_qr.status_code == 201
    first = first_qr.json()

    second_qr = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=_bearer(first["access_token"]),
        json={
            "code": DEMO_QR_CODE,
            "locale": "en",
            "client_type": "ios",
            "device_id": "second-qr",
        },
    )
    assert second_qr.status_code == 201
    second = second_qr.json()

    replaced_access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(first["access_token"]),
    )
    assert replaced_access.status_code == 401
    assert replaced_access.json()["error"]["code"] == "invalid_session"
    replaced_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": first["refresh_token"]},
    )
    assert replaced_refresh.status_code == 401
    assert replaced_refresh.json()["error"]["code"] == "invalid_refresh_token"

    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(second["access_token"]),
    ).status_code == 200
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(full["access_token"]),
    ).status_code == 200
    full_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": full["refresh_token"]},
    )
    assert full_refresh.status_code == 200


def test_anonymous_to_qr_exchange_replaces_the_foreground_guest_session(
    client: TestClient,
) -> None:
    anonymous = client.post(
        f"{API_V1}/auth/anonymous",
        json={
            "locale": "en",
            "client_type": "ios",
            "device_id": "anonymous-foreground",
        },
    )
    assert anonymous.status_code == 201
    guest = anonymous.json()

    exchanged = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=_bearer(guest["access_token"]),
        json={
            "code": DEMO_QR_CODE,
            "locale": "en",
            "client_type": "ios",
            "device_id": "qr-foreground",
        },
    )
    assert exchanged.status_code == 201
    qr = exchanged.json()

    old_access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(guest["access_token"]),
    )
    assert old_access.status_code == 401
    assert old_access.json()["error"]["code"] == "invalid_session"
    old_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": guest["refresh_token"]},
    )
    assert old_refresh.status_code == 401
    assert old_refresh.json()["error"]["code"] == "invalid_refresh_token"

    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(qr["access_token"]),
    ).status_code == 200
    assert client.get(
        f"{API_V1}/sessions/current/bootstrap",
        headers=_bearer(qr["access_token"]),
    ).status_code == 200


def test_qr_bootstrap_excludes_unavailable_menu_items(client: TestClient) -> None:
    menu = client.get(f"{API_V1}/restaurants/halmonis-table/menu").json()
    japchae = next(
        item
        for category in menu["categories"]
        for item in category["items"]
        if item["slug"] == "japchae"
    )
    restaurant_id = menu["restaurant"]["id"]
    login = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": "owner@fofu.app",
            "password": DEMO_PASSWORD,
            "client_type": "ios",
        },
    )
    assert login.status_code == 200
    owner_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    availability_url = (
        f"{API_V1}/owner/restaurants/{restaurant_id}/menu-items/"
        f"{japchae['id']}/availability"
    )
    disabled = client.patch(
        availability_url, headers=owner_headers, json={"is_available": False}
    )
    assert disabled.status_code == 200

    try:
        exchange = client.post(
            f"{API_V1}/guest-sessions/qr",
            json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
        )
        assert exchange.status_code == 201
        slugs = {
            item["slug"]
            for category in exchange.json()["bootstrap"]["menu"]
            for item in category["items"]
        }
        assert "japchae" not in slugs
        assert "samgyeopsal" in slugs
    finally:
        restored = client.patch(
            availability_url, headers=owner_headers, json={"is_available": True}
        )
        assert restored.status_code == 200


def test_unknown_price_menu_can_be_prepared_without_fake_totals(
    client: TestClient,
) -> None:
    with SessionLocal() as db:
        item = db.scalar(select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal"))
        assert item is not None
        original_price = item.price_amount
        item.price_amount = None
        db.commit()

    try:
        exchange_response = client.post(
            f"{API_V1}/guest-sessions/qr",
            json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
        )
        assert exchange_response.status_code == 201
        exchange = exchange_response.json()
        bootstrap = exchange["bootstrap"]
        unknown_price_item = _find_item(bootstrap, "samgyeopsal")
        assert "price" not in unknown_price_item
        assert unknown_price_item["is_orderable"] is True
        assert unknown_price_item["orderability_reason"] == "price_unknown"

        cart = bootstrap["cart"]
        added_unknown = client.put(
            f"{API_V1}/cart/items/{unknown_price_item['id']}",
            headers=_bearer(exchange["access_token"]),
            json={
                "quantity": 1,
                "expected_version": cart["version"],
                "expected_menu_revision": cart["menu_revision"],
            },
        )
        assert added_unknown.status_code == 200, added_unknown.text
        cart = added_unknown.json()
        unknown_cart_item = next(
            item for item in cart["items"] if item["menu_item_id"] == unknown_price_item["id"]
        )
        assert unknown_cart_item["unit_price"] is None
        assert unknown_cart_item["line_total"] is None
        assert cart["subtotal"] is None

        # A known-price row may retain its own price, but an aggregate over a
        # mixed cart must stay unknown instead of presenting a partial total.
        known_price_item = _find_item(bootstrap, "japchae")
        added_known = client.put(
            f"{API_V1}/cart/items/{known_price_item['id']}",
            headers=_bearer(exchange["access_token"]),
            json={
                "quantity": 2,
                "expected_version": cart["version"],
                "expected_menu_revision": cart["menu_revision"],
            },
        )
        assert added_known.status_code == 200, added_known.text
        cart = added_known.json()
        known_cart_item = next(
            item for item in cart["items"] if item["menu_item_id"] == known_price_item["id"]
        )
        assert known_cart_item["unit_price"]["amount"] == known_price_item["price"]["amount"]
        assert known_cart_item["line_total"]["amount"] == (
            known_price_item["price"]["amount"] * 2
        )
        assert cart["subtotal"] is None

        headers = _bearer(exchange["access_token"])
        preview = client.get(
            f"{API_V1}/cart/order-card",
            headers=headers,
            params={
                "expected_version": cart["version"],
                "expected_menu_revision": cart["menu_revision"],
            },
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        unknown_order_item = next(
            item
            for item in preview_body["items"]
            if item["menu_item_id"] == unknown_price_item["id"]
        )
        assert unknown_order_item["unit_price"] is None
        assert unknown_order_item["line_total"] is None
        assert preview_body["subtotal"] is None
        assert preview_body["total"] is None
        assert "삼겹살 1개" in preview_body["korean_phrase"]
        assert "Samgyeopsal × 1" in preview_body["translated_phrase"]

        request = {
            "idempotency_key": "unknown-price-order-card-regression",
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
            "locale": "en",
        }
        prepared = client.post(
            f"{API_V1}/cart/order-card", headers=headers, json=request
        )
        replayed = client.post(
            f"{API_V1}/cart/order-card", headers=headers, json=request
        )
        assert prepared.status_code == replayed.status_code == 201
        assert prepared.json() == replayed.json()
        assert prepared.json()["subtotal"] is None
        assert prepared.json()["total"] is None

        history = client.get(f"{API_V1}/me/orders", headers=headers)
        assert history.status_code == 200, history.text
        history_order = next(
            order
            for order in history.json()["items"]
            if order["id"] == prepared.json()["order_id"]
        )
        assert history_order["subtotal_amount"] is None
        assert history_order["total_amount"] is None
        history_unknown_item = next(
            item
            for item in history_order["items"]
            if item["menu_item_id"] == unknown_price_item["id"]
        )
        assert history_unknown_item["unit_price_amount"] is None
        assert history_unknown_item["line_total_amount"] is None
    finally:
        with SessionLocal() as db:
            item = db.scalar(
                select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal")
            )
            assert item is not None
            item.price_amount = original_price
            db.commit()


def test_unavailable_menu_item_still_cannot_be_added_to_cart(client: TestClient) -> None:
    exchange = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
    )
    assert exchange.status_code == 201
    body = exchange.json()
    item = _find_item(body["bootstrap"], "samgyeopsal")

    with SessionLocal() as db:
        stored_item = db.get(models.MenuItem, item["id"])
        assert stored_item is not None
        stored_item.is_available = False
        db.commit()

    try:
        cart = body["bootstrap"]["cart"]
        response = client.put(
            f"{API_V1}/cart/items/{item['id']}",
            headers=_bearer(body["access_token"]),
            json={
                "quantity": 1,
                "expected_version": cart["version"],
                "expected_menu_revision": cart["menu_revision"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "menu_item_unavailable"
    finally:
        with SessionLocal() as db:
            stored_item = db.get(models.MenuItem, item["id"])
            assert stored_item is not None
            stored_item.is_available = True
            db.commit()


def test_zero_price_remains_a_known_price(client: TestClient) -> None:
    with SessionLocal() as db:
        item = db.scalar(select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal"))
        assert item is not None
        original_price = item.price_amount
        item.price_amount = 0
        db.commit()

    try:
        exchange_response = client.post(
            f"{API_V1}/guest-sessions/qr",
            json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
        )
        assert exchange_response.status_code == 201
        exchange = exchange_response.json()
        item = _find_item(exchange["bootstrap"], "samgyeopsal")
        assert item["price"]["amount"] == 0
        assert item["is_orderable"] is True
        assert "orderability_reason" not in item

        cart = exchange["bootstrap"]["cart"]
        response = client.put(
            f"{API_V1}/cart/items/{item['id']}",
            headers=_bearer(exchange["access_token"]),
            json={
                "quantity": 1,
                "expected_version": cart["version"],
                "expected_menu_revision": cart["menu_revision"],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"][0]["unit_price"]["amount"] == 0
        assert body["items"][0]["line_total"]["amount"] == 0
        assert body["subtotal"]["amount"] == 0
    finally:
        with SessionLocal() as db:
            item = db.scalar(
                select(models.MenuItem).where(models.MenuItem.slug == "samgyeopsal")
            )
            assert item is not None
            item.price_amount = original_price
            db.commit()


def test_cart_requires_qr_scope_and_preserves_restaurant_binding(client: TestClient) -> None:
    login = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": "demo@fofu.app",
            "password": DEMO_PASSWORD,
            "client_type": "ios",
        },
    )
    assert login.status_code == 200
    full_headers = _bearer(login.json()["access_token"])

    exchange = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=full_headers,
        json={
            "code": DEMO_QR_CODE,
            "locale": "en",
            "client_type": "ios",
            "device_id": "cart-scope-regression-device",
        },
    )
    assert exchange.status_code == 201
    exchange_body = exchange.json()
    qr_headers = _bearer(exchange_body["access_token"])
    restaurant_id = exchange_body["bootstrap"]["session"]["restaurant_id"]
    item = _find_item(exchange_body["bootstrap"], "samgyeopsal")

    # The original full session remains valid, but it must never fall back to the
    # registered user's most recently updated QR cart.
    full_scope_requests = (
        ("GET", f"{API_V1}/cart", None),
        (
            "PUT",
            f"{API_V1}/cart/items/{item['id']}",
            {"quantity": 1},
        ),
        (
            "DELETE",
            f"{API_V1}/cart/items/{item['id']}",
            None,
        ),
        (
            "PATCH",
            f"{API_V1}/cart",
            {"serving_mode": "takeout"},
        ),
        ("GET", f"{API_V1}/cart/order-card", None),
        (
            "POST",
            f"{API_V1}/cart/order-card",
            {"idempotency_key": "full-scope-must-be-rejected"},
        ),
    )
    for method, url, payload in full_scope_requests:
        response = client.request(method, url, headers=full_headers, json=payload)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "qr_session_required"

    cart_response = client.get(f"{API_V1}/cart", headers=qr_headers)
    assert cart_response.status_code == 200
    cart = cart_response.json()
    assert cart["restaurant_id"] == restaurant_id

    other_menu = client.get(f"{API_V1}/restaurants/green-bowl/menu")
    assert other_menu.status_code == 200
    other_item = next(
        menu_item
        for category in other_menu.json()["categories"]
        for menu_item in category["items"]
    )
    cross_restaurant_add = client.put(
        f"{API_V1}/cart/items/{other_item['id']}",
        headers=qr_headers,
        json={"quantity": 1},
    )
    assert cross_restaurant_add.status_code == 404
    assert cross_restaurant_add.json()["error"]["code"] == "menu_item_not_found"
    still_scoped = client.get(f"{API_V1}/cart", headers=qr_headers)
    assert still_scoped.status_code == 200
    assert still_scoped.json()["restaurant_id"] == restaurant_id

    serving_mode = "takeout" if cart["serving_mode"] == "dine_in" else "dine_in"
    patched = client.patch(
        f"{API_V1}/cart",
        headers=qr_headers,
        json={
            "serving_mode": serving_mode,
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert patched.status_code == 200
    cart = patched.json()
    assert cart["restaurant_id"] == restaurant_id

    added = client.put(
        f"{API_V1}/cart/items/{item['id']}",
        headers=qr_headers,
        json={
            "quantity": 1,
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert added.status_code == 200
    cart = added.json()
    assert cart["restaurant_id"] == restaurant_id

    preview = client.get(f"{API_V1}/cart/order-card", headers=qr_headers)
    assert preview.status_code == 200
    assert preview.json()["restaurant_id"] == restaurant_id

    prepared = client.post(
        f"{API_V1}/cart/order-card",
        headers=qr_headers,
        json={
            "idempotency_key": "qr-scope-order-card-regression",
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert prepared.status_code == 201
    assert prepared.json()["restaurant_id"] == restaurant_id

    deleted = client.delete(
        f"{API_V1}/cart/items/{item['id']}",
        headers=qr_headers,
        params={
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["restaurant_id"] == restaurant_id


def test_cart_versions_compatibility_and_order_idempotency(client: TestClient) -> None:
    exchange = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
    ).json()
    headers = _bearer(exchange["access_token"])
    bootstrap = exchange["bootstrap"]
    samgyeopsal = _find_item(bootstrap, "samgyeopsal")
    japchae = _find_item(bootstrap, "japchae")
    initial_cart = bootstrap["cart"]

    passport = client.patch(
        f"{API_V1}/me/passport",
        headers=headers,
        json={"version": 1, "avoid_ingredient_codes": ["pork"]},
    )
    assert passport.status_code == 200

    added = client.put(
        f"{API_V1}/cart/items/{samgyeopsal['id']}",
        headers=headers,
        json={
            "quantity": 1,
            "request_codes": ["no_pork"],
            "expected_version": initial_cart["version"],
            "expected_menu_revision": initial_cart["menu_revision"],
        },
    )
    assert added.status_code == 200
    cart = added.json()
    assert cart["version"] == initial_cart["version"] + 1
    assert cart["subtotal"]["amount"] == samgyeopsal["price"]["amount"]

    stale = client.put(
        f"{API_V1}/cart/items/{japchae['id']}",
        headers=headers,
        json={"quantity": 1, "expected_version": initial_cart["version"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "cart_version_conflict"

    preview = client.get(
        f"{API_V1}/cart/order-card",
        headers=headers,
        params={
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert preview.status_code == 200
    assert preview.json()["compatibility"]["status"] == "conflict"
    assert preview.json()["request_note_ko"] == "돼지고기는 빼 주세요"

    request = {
        "idempotency_key": "qr-order-integration-0001",
        "expected_version": cart["version"],
        "expected_menu_revision": cart["menu_revision"],
        "locale": "en",
    }
    prepared = client.post(f"{API_V1}/cart/order-card", headers=headers, json=request)
    replayed = client.post(f"{API_V1}/cart/order-card", headers=headers, json=request)
    assert prepared.status_code == replayed.status_code == 201
    assert prepared.json()["status"] == "prepared"
    assert prepared.json() == replayed.json()

    changed = client.put(
        f"{API_V1}/cart/items/{samgyeopsal['id']}",
        headers=headers,
        json={
            "quantity": 2,
            "request_codes": ["no_pork"],
            "expected_version": cart["version"],
            "expected_menu_revision": cart["menu_revision"],
        },
    )
    assert changed.status_code == 200
    changed_cart = changed.json()

    # An identical retry returns the immutable first response without revalidating
    # the now-stale cart version or regenerating generated_at.
    replayed_after_change = client.post(
        f"{API_V1}/cart/order-card", headers=headers, json=request
    )
    assert replayed_after_change.status_code == 201
    assert replayed_after_change.json() == prepared.json()

    conflicting_replay = client.post(
        f"{API_V1}/cart/order-card",
        headers=headers,
        json={**request, "expected_version": changed_cart["version"]},
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["error"]["code"] == "idempotency_key_conflict"

    history = client.get(f"{API_V1}/me/orders", headers=headers)
    assert history.status_code == 200
    assert [order["id"] for order in history.json()["items"]] == [
        prepared.json()["order_id"]
    ]
    assert history.json()["items"][0]["table_label"] == prepared.json()["table_label"]
