from __future__ import annotations

import stat
import uuid
from pathlib import Path

from conftest import API_V1, DEMO_PASSWORD, DEMO_QR_CODE
from fastapi.testclient import TestClient

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.security import hash_password


def _login(client: TestClient, email: str) -> tuple[dict[str, str], dict[str, object]]:
    response = client.post(
        f"{API_V1}/auth/login",
        json={"email": email, "password": DEMO_PASSWORD, "client_type": "ios"},
    )
    assert response.status_code == 200
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


def _register(client: TestClient) -> tuple[dict[str, str], dict[str, object]]:
    response = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": f"integration-{uuid.uuid4().hex}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Integration Applicant",
            "locale": "en",
            "client_type": "ios",
        },
    )
    assert response.status_code == 201
    body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


def _create_admin() -> str:
    email = f"owner-review-admin-{uuid.uuid4().hex}@example.com"
    with SessionLocal() as db:
        db.add(
            models.User(
                email=email,
                password_hash=hash_password(DEMO_PASSWORD),
                display_name="Owner Review Admin",
                locale="en",
                is_guest=False,
                is_active=True,
                roles=["admin"],
            )
        )
        db.commit()
    return email


def _halmoni_restaurant_id(client: TestClient) -> str:
    items = client.get(f"{API_V1}/restaurants").json()["items"]
    return next(item["id"] for item in items if item["slug"] == "halmonis-table")


def test_owner_authorization_and_qr_secret_lifecycle(client: TestClient) -> None:
    restaurant_id = _halmoni_restaurant_id(client)
    owner_headers, _ = _login(client, "owner@fofu.app")
    customer_headers, customer_auth = _login(client, "demo@fofu.app")

    dashboard = client.get(
        f"{API_V1}/owner/restaurants/{restaurant_id}/dashboard", headers=owner_headers
    )
    assert dashboard.status_code == 200
    assert dashboard.json()["membership_role"] == "owner"
    assert dashboard.json()["restaurant"]["id"] == restaurant_id

    initial_open = dashboard.json()["restaurant"]["is_open"]
    open_status_url = f"{API_V1}/owner/restaurants/{restaurant_id}/open-status"
    changed_open = client.patch(
        open_status_url,
        headers=owner_headers,
        json={"is_open": not initial_open},
    )
    assert changed_open.status_code == 200
    assert changed_open.json()["is_open"] is not initial_open
    try:
        refreshed_dashboard = client.get(
            f"{API_V1}/owner/restaurants/{restaurant_id}/dashboard",
            headers=owner_headers,
        )
        assert refreshed_dashboard.status_code == 200
        assert refreshed_dashboard.json()["restaurant"]["is_open"] is not initial_open
    finally:
        restored_open = client.patch(
            open_status_url,
            headers=owner_headers,
            json={"is_open": initial_open},
        )
        assert restored_open.status_code == 200
        assert restored_open.json()["is_open"] is initial_open

    initial_hours = dashboard.json()["hours"]
    first_open_day = next(hour for hour in initial_hours if not hour["is_closed"])
    hours_url = f"{API_V1}/owner/restaurants/{restaurant_id}/hours"
    changed_hours = client.patch(
        hours_url,
        headers=owner_headers,
        json={
            "hours": [
                {
                    "day_of_week": first_open_day["day_of_week"],
                    "opens_at": "10:30:00",
                    "closes_at": first_open_day["closes_at"],
                    "is_closed": False,
                }
            ]
        },
    )
    assert changed_hours.status_code == 200
    changed_day = next(
        hour
        for hour in changed_hours.json()
        if hour["day_of_week"] == first_open_day["day_of_week"]
    )
    assert changed_day["opens_at"] == "10:30:00"
    try:
        refreshed_dashboard = client.get(
            f"{API_V1}/owner/restaurants/{restaurant_id}/dashboard",
            headers=owner_headers,
        )
        assert refreshed_dashboard.status_code == 200
        assert any(
            hour["day_of_week"] == first_open_day["day_of_week"]
            and hour["opens_at"] == "10:30:00"
            for hour in refreshed_dashboard.json()["hours"]
        )
    finally:
        restored_hours = client.patch(
            hours_url,
            headers=owner_headers,
            json={
                "hours": [
                    {
                        "day_of_week": hour["day_of_week"],
                        "opens_at": hour["opens_at"],
                        "closes_at": hour["closes_at"],
                        "is_closed": hour["is_closed"],
                    }
                    for hour in initial_hours
                ]
            },
        )
        assert restored_hours.status_code == 200

    managed = client.get(f"{API_V1}/owner/restaurants", headers=owner_headers)
    assert managed.status_code == 200
    assert [item["id"] for item in managed.json()["items"]] == [restaurant_id]

    other_restaurant_id = next(
        item["id"]
        for item in client.get(f"{API_V1}/restaurants").json()["items"]
        if item["id"] != restaurant_id
    )
    with SessionLocal() as db:
        db.add(
            models.RestaurantMembership(
                restaurant_id=other_restaurant_id,
                user_id=customer_auth["user"]["id"],
                role="manager",
                status="revoked",
            )
        )
        db.commit()

    customer_managed = client.get(
        f"{API_V1}/owner/restaurants", headers=customer_headers
    )
    assert customer_managed.status_code == 200
    assert customer_managed.json() == {"items": []}

    forbidden = client.get(
        f"{API_V1}/owner/restaurants/{restaurant_id}/dashboard", headers=customer_headers
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "restaurant_access_forbidden"

    created = client.post(
        f"{API_V1}/owner/restaurants/{restaurant_id}/qr-codes",
        headers=owner_headers,
        json={"label": "Patio table", "table_label": "P-7", "purpose": "table_entry"},
    )
    assert created.status_code == 201
    qr = created.json()
    assert len(qr["code"]) >= 16
    assert qr["web_url"].endswith(f"/q/{qr['code']}")

    listed = client.get(
        f"{API_V1}/owner/restaurants/{restaurant_id}/qr-codes", headers=owner_headers
    )
    assert listed.status_code == 200
    listed_row = next(item for item in listed.json()["items"] if item["id"] == qr["id"])
    assert "code" not in listed_row
    assert listed_row["public_hint"] == qr["public_hint"]

    resolved = client.get(f"{API_V1}/qr/{qr['code']}")
    assert resolved.status_code == 200
    assert resolved.json()["table_label"] == "P-7"

    table_session = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={"code": qr["code"], "locale": "en", "client_type": "web"},
    )
    assert table_session.status_code == 201
    assert table_session.json()["bootstrap"]["session"]["table_label"] == "P-7"
    assert table_session.json()["bootstrap"]["cart"]["table_label"] == "P-7"

    revoked = client.delete(
        f"{API_V1}/owner/restaurants/{restaurant_id}/qr-codes/{qr['id']}",
        headers=owner_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"id": qr["id"], "revoked": True}
    after_revoke = client.get(f"{API_V1}/qr/{qr['code']}")
    assert after_revoke.status_code == 404
    assert after_revoke.json()["error"]["code"] == "qr_not_found"


def test_staff_membership_is_dashboard_read_only(client: TestClient) -> None:
    staff_headers, staff_auth = _register(client)
    restaurant = next(
        item
        for item in client.get(f"{API_V1}/restaurants").json()["items"]
        if item["slug"] != "halmonis-table"
    )
    with SessionLocal() as db:
        db.add(
            models.RestaurantMembership(
                restaurant_id=restaurant["id"],
                user_id=staff_auth["user"]["id"],
                role="staff",
                status="active",
            )
        )
        db.commit()

    dashboard = client.get(
        f"{API_V1}/owner/restaurants/{restaurant['id']}/dashboard",
        headers=staff_headers,
    )
    assert dashboard.status_code == 200
    assert dashboard.json()["membership_role"] == "staff"

    forbidden_open = client.patch(
        f"{API_V1}/owner/restaurants/{restaurant['id']}/open-status",
        headers=staff_headers,
        json={"is_open": not dashboard.json()["restaurant"]["is_open"]},
    )
    assert forbidden_open.status_code == 403
    assert forbidden_open.json()["error"]["code"] == "restaurant_access_forbidden"

    forbidden_hours = client.patch(
        f"{API_V1}/owner/restaurants/{restaurant['id']}/hours",
        headers=staff_headers,
        json={
            "hours": [
                {
                    "day_of_week": 0,
                    "opens_at": "09:00:00",
                    "closes_at": "21:00:00",
                    "is_closed": False,
                }
            ]
        },
    )
    assert forbidden_hours.status_code == 403
    assert forbidden_hours.json()["error"]["code"] == "restaurant_access_forbidden"

    menu_item = dashboard.json()["menu_items"][0]
    forbidden_menu = client.patch(
        f"{API_V1}/owner/restaurants/{restaurant['id']}/menu-items/"
        f"{menu_item['id']}/availability",
        headers=staff_headers,
        json={"is_available": not menu_item["is_available"]},
    )
    assert forbidden_menu.status_code == 403
    assert forbidden_menu.json()["error"]["code"] == "restaurant_access_forbidden"


def test_messages_enforce_participants_filters_and_idempotency(client: TestClient) -> None:
    demo_headers, _ = _login(client, "demo@fofu.app")

    restaurants = client.get(
        f"{API_V1}/conversations",
        headers=demo_headers,
        params={"filter": "restaurants"},
    )
    unread = client.get(
        f"{API_V1}/conversations", headers=demo_headers, params={"filter": "unread"}
    )
    searched = client.get(
        f"{API_V1}/conversations", headers=demo_headers, params={"q": "Halmoni"}
    )
    assert restaurants.status_code == unread.status_code == searched.status_code == 200
    conversation = restaurants.json()["items"][0]
    conversation_id = conversation["id"]
    assert conversation["restaurant_id"] == _halmoni_restaurant_id(client)
    assert any(item["id"] == conversation_id for item in unread.json()["items"])
    assert any(item["id"] == conversation_id for item in searched.json()["items"])

    messages = client.get(
        f"{API_V1}/conversations/{conversation_id}/messages", headers=demo_headers
    )
    assert messages.status_code == 200
    assert len(messages.json()["items"]) >= 4

    payload = {
        "body": "Is the patio open today?",
        "kind": "text",
        "client_message_id": f"integration-{uuid.uuid4().hex}",
    }
    sent = client.post(
        f"{API_V1}/conversations/{conversation_id}/messages",
        headers=demo_headers,
        json=payload,
    )
    replayed = client.post(
        f"{API_V1}/conversations/{conversation_id}/messages",
        headers=demo_headers,
        json=payload,
    )
    assert sent.status_code == replayed.status_code == 201
    assert sent.json()["id"] == replayed.json()["id"]
    assert sent.json()["idempotency_replayed"] is False
    assert replayed.json()["idempotency_replayed"] is True

    conflict = client.post(
        f"{API_V1}/conversations/{conversation_id}/messages",
        headers=demo_headers,
        json={**payload, "body": "Different content"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "client_message_id_conflict"

    outsider_headers, _ = _register(client)
    hidden = client.get(
        f"{API_V1}/conversations/{conversation_id}/messages", headers=outsider_headers
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "conversation_not_found"


def test_qr_guest_messaging_is_limited_to_scoped_restaurant(client: TestClient) -> None:
    exchange = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={"code": DEMO_QR_CODE, "locale": "en", "client_type": "ios"},
    ).json()
    headers = {"Authorization": f"Bearer {exchange['access_token']}"}
    scoped_id = exchange["bootstrap"]["restaurant"]["id"]

    allowed = client.post(
        f"{API_V1}/conversations",
        headers=headers,
        json={"kind": "restaurant", "restaurant_id": scoped_id, "title": "Table help"},
    )
    assert allowed.status_code == 201
    assert allowed.json()["restaurant_id"] == scoped_id

    other_id = next(
        item["id"]
        for item in client.get(f"{API_V1}/restaurants").json()["items"]
        if item["id"] != scoped_id
    )
    forbidden = client.post(
        f"{API_V1}/conversations",
        headers=headers,
        json={"kind": "restaurant", "restaurant_id": other_id},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "guest_messaging_scope_forbidden"

    guest_upload = client.post(
        f"{API_V1}/media/uploads",
        headers=headers,
        data={"purpose": "business_license"},
        files={"file": ("license.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert guest_upload.status_code == 403
    assert guest_upload.json()["error"]["code"] == "registered_account_required"


def test_private_license_upload_and_owner_application(
    client: TestClient, upload_root: Path
) -> None:
    headers, _ = _register(client)

    # New venues are imported as unpublished drafts. They must still be
    # selectable for an application; publication happens only after review.
    draft_slug = f"application-draft-{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        draft = models.Restaurant(
            slug=draft_slug,
            name_en="Application Draft Restaurant",
            handle=f"@{draft_slug}",
            category="Application test",
            address_en="1 Application Test-ro, Seoul",
            latitude=37.5563,
            longitude=126.9236,
            is_verified=False,
            is_open=False,
            is_published=False,
        )
        db.add(draft)
        db.commit()
        draft_id = draft.id

    png = (
        b"\x89PNG\r\n\x1a\n"
        b"integration-test-image"
        b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
    )
    uploaded = client.post(
        f"{API_V1}/media/uploads",
        headers=headers,
        data={"purpose": "business_license"},
        files={"file": ("license.png", png, "image/png")},
    )
    assert uploaded.status_code == 201
    asset = uploaded.json()
    assert asset["purpose"] == "business_license"
    assert asset["content_type"] == "image/png"
    assert asset["size_bytes"] == len(png)

    with SessionLocal() as db:
        stored = db.get(models.MediaAsset, asset["id"])
        assert stored is not None
        private_path = upload_root / stored.storage_key
    assert private_path.is_file()
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600

    spoofed = client.post(
        f"{API_V1}/media/uploads",
        headers=headers,
        data={"purpose": "business_license"},
        files={"file": ("spoofed.png", b"not really png", "image/png")},
    )
    assert spoofed.status_code == 415
    assert spoofed.json()["error"]["code"] == "file_signature_mismatch"

    candidates = client.get(
        f"{API_V1}/owner/application-restaurants", headers=headers
    )
    assert candidates.status_code == 200
    candidate_items = candidates.json()["items"]
    assert candidate_items
    assert all(item["slug"] != "halmonis-table" for item in candidate_items)
    selected_restaurant = next(
        item for item in candidate_items if item["id"] == draft_id
    )
    assert selected_restaurant["is_published"] is False

    searched_candidates = client.get(
        f"{API_V1}/owner/application-restaurants",
        headers=headers,
        params={"q": selected_restaurant["name_en"]},
    )
    assert searched_candidates.status_code == 200
    assert any(
        item["id"] == selected_restaurant["id"]
        for item in searched_candidates.json()["items"]
    )

    registration_number = f"INT-{uuid.uuid4().hex[:12].upper()}"
    missing_restaurant = client.post(
        f"{API_V1}/owner/applications",
        headers=headers,
        json={
            "business_name": "Integration Kitchen",
            "registration_number": registration_number,
            "address": "12 Test-ro, Seoul",
            "phone": "+82 2 1234 5678",
            "license_media_id": asset["id"],
            "agreed_to_terms": True,
            "terms_version": "2026.08",
        },
    )
    assert missing_restaurant.status_code == 422

    application = client.post(
        f"{API_V1}/owner/applications",
        headers=headers,
        json={
            "restaurant_id": selected_restaurant["id"],
            "business_name": "Integration Kitchen",
            "registration_number": registration_number,
            "address": "12 Test-ro, Seoul",
            "phone": "+82 2 1234 5678",
            "license_media_id": asset["id"],
            "agreed_to_terms": True,
            "terms_version": "2026.08",
        },
    )
    assert application.status_code == 201
    body = application.json()
    assert body["status"] == "pending"
    assert body["phone_verified_at"] is None
    assert body["registration_number"] == registration_number
    assert body["restaurant_id"] == selected_restaurant["id"]

    statuses = client.get(f"{API_V1}/owner/applications/status", headers=headers)
    assert statuses.status_code == 200
    assert [item["id"] for item in statuses.json()["items"]] == [body["id"]]

    admin_headers, _ = _login(client, _create_admin())
    approved = client.patch(
        f"{API_V1}/admin/owner-applications/{body['id']}/review",
        headers=admin_headers,
        json={"status": "approved", "review_note": "License verified."},
    )
    assert approved.status_code == 200
    assert approved.json()["restaurant_id"] == selected_restaurant["id"]

    approved_status = client.get(
        f"{API_V1}/owner/applications/status", headers=headers
    )
    assert approved_status.status_code == 200
    assert approved_status.json()["items"][0]["status"] == "approved"

    managed = client.get(f"{API_V1}/owner/restaurants", headers=headers)
    assert managed.status_code == 200
    assert [item["id"] for item in managed.json()["items"]] == [
        selected_restaurant["id"]
    ]


def test_upload_refuses_to_repermission_an_existing_broad_directory(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    headers, _ = _register(client)
    unsafe_root = tmp_path / "shared-directory"
    unsafe_root.mkdir(mode=0o755)
    unsafe_root.chmod(0o755)
    monkeypatch.setattr(get_settings(), "upload_dir", unsafe_root)

    response = client.post(
        f"{API_V1}/media/uploads",
        headers=headers,
        data={"purpose": "business_license"},
        files={"file": ("license.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "upload_storage_error"
    assert stat.S_IMODE(unsafe_root.stat().st_mode) == 0o755
