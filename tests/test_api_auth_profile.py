from __future__ import annotations

import uuid
from datetime import datetime, timezone

from conftest import API_V1, DEMO_QR_CODE
from fastapi.testclient import TestClient


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_utc_rfc3339(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_register_login_and_profile_timestamps_are_explicit_utc(client: TestClient) -> None:
    email = f"timestamps-{uuid.uuid4().hex}@example.com"
    password = "correct-horse-battery-staple"

    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": email,
            "password": password,
            "display_name": "Timestamp Test",
            "locale": "en",
            "client_type": "ios",
        },
    )
    assert registered.status_code == 201
    registered_body = registered.json()
    _assert_utc_rfc3339(registered_body["refresh_expires_at"])

    profile = client.get(
        f"{API_V1}/me",
        headers=_bearer(registered_body["access_token"]),
    )
    assert profile.status_code == 200
    profile_body = profile.json()
    _assert_utc_rfc3339(profile_body["created_at"])
    _assert_utc_rfc3339(profile_body["updated_at"])

    logged_in = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": email,
            "password": password,
            "client_type": "ios",
        },
    )
    assert logged_in.status_code == 200
    _assert_utc_rfc3339(logged_in.json()["refresh_expires_at"])


def test_native_refresh_rotation_rejects_replay(client: TestClient) -> None:
    created = client.post(
        f"{API_V1}/auth/anonymous",
        json={"client_type": "ios", "locale": "fr", "device_id": "ios-test-device"},
    )
    assert created.status_code == 201
    first = created.json()
    assert first["scope"] == "guest"
    assert first["refresh_token"]

    rotated = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": first["refresh_token"]},
    )
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["user"]["id"] == first["user"]["id"]

    replay = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_refresh_token"


def test_web_refresh_uses_hardened_http_only_cookie(client: TestClient) -> None:
    created = client.post(
        f"{API_V1}/auth/anonymous",
        json={"client_type": "web", "locale": "en"},
    )
    assert created.status_code == 201
    assert "refresh_token" not in created.json()
    cookie = created.headers["set-cookie"]
    assert "fofu_refresh_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1" in cookie
    previous_cookie_value = client.cookies["fofu_refresh_token"]

    refreshed = client.post(f"{API_V1}/auth/refresh")
    assert refreshed.status_code == 200
    assert "refresh_token" not in refreshed.json()
    assert client.cookies["fofu_refresh_token"] != previous_cookie_value


def test_guest_upgrade_preserves_passport_and_saved_restaurants(client: TestClient) -> None:
    guest = client.post(
        f"{API_V1}/auth/anonymous",
        json={"client_type": "ios", "locale": "en", "display_name": "Before Upgrade"},
    ).json()
    guest_headers = _bearer(guest["access_token"])
    guest_user_id = guest["user"]["id"]
    anonymous_refresh_token = guest["refresh_token"]

    passport = client.patch(
        f"{API_V1}/me/passport",
        headers=guest_headers,
        json={
            "version": 1,
            "avoid_ingredient_codes": ["pork"],
            "liked_ingredient_codes": ["rice", "tofu"],
        },
    )
    assert passport.status_code == 200
    assert passport.json()["version"] == 2

    restaurant_id = client.get(f"{API_V1}/restaurants").json()["items"][0]["id"]
    saved = client.put(
        f"{API_V1}/me/saved-restaurants/{restaurant_id}", headers=guest_headers
    )
    assert saved.status_code == 200

    qr_session = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=guest_headers,
        json={"code": DEMO_QR_CODE, "client_type": "ios", "locale": "en"},
    )
    assert qr_session.status_code == 201
    qr_body = qr_session.json()
    assert qr_body["user"]["id"] == guest_user_id
    qr_headers = _bearer(qr_body["access_token"])

    email = f"upgrade-{uuid.uuid4().hex}@example.com"
    upgraded = client.post(
        f"{API_V1}/auth/register",
        headers=qr_headers,
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": "After Upgrade",
            "locale": "fr",
            "home_country_code": "fr",
            "client_type": "ios",
        },
    )
    assert upgraded.status_code == 201
    upgraded_body = upgraded.json()
    assert upgraded_body["user"]["id"] == guest_user_id
    assert upgraded_body["user"]["is_guest"] is False
    assert upgraded_body["user"]["home_country_code"] == "FR"
    upgraded_headers = _bearer(upgraded_body["access_token"])

    old_session = client.get(f"{API_V1}/auth/me", headers=guest_headers)
    assert old_session.status_code == 401
    assert old_session.json()["error"]["code"] == "invalid_session"
    old_qr_session = client.get(f"{API_V1}/auth/me", headers=qr_headers)
    assert old_qr_session.status_code == 401
    assert old_qr_session.json()["error"]["code"] == "invalid_session"

    for stale_refresh_token in (anonymous_refresh_token, qr_body["refresh_token"]):
        stale_refresh = client.post(
            f"{API_V1}/auth/refresh",
            json={"refresh_token": stale_refresh_token},
        )
        assert stale_refresh.status_code == 401
        assert stale_refresh.json()["error"]["code"] == "invalid_refresh_token"

    preserved_passport = client.get(f"{API_V1}/me/passport", headers=upgraded_headers)
    assert preserved_passport.status_code == 200
    assert preserved_passport.json()["avoid_ingredient_codes"] == ["pork"]
    assert preserved_passport.json()["liked_ingredient_codes"] == ["rice", "tofu"]

    preserved_saved = client.get(
        f"{API_V1}/me/saved-restaurants", headers=upgraded_headers
    )
    assert preserved_saved.status_code == 200
    assert [item["id"] for item in preserved_saved.json()["items"]] == [restaurant_id]

    stale = client.patch(
        f"{API_V1}/me/passport",
        headers=upgraded_headers,
        json={"version": 1, "spice_tolerance": 4},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "passport_version_conflict"
