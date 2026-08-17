from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import API_V1, DEMO_QR_CODE
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models
from app.config import Settings
from app.database import SessionLocal
from app.services import push as push_service


def _push_settings(*, max_devices: int = 10) -> Settings:
    return Settings(
        _env_file=None,
        apns_enabled=True,
        apns_environment="sandbox",
        apns_team_id="TESTTEAM01",
        apns_key_id="TESTKEY001",
        apns_bundle_id="im.fofu.fofu",
        apns_private_key_path=Path("/private/tmp/unused-apns-test-key.p8"),
        push_max_active_devices_per_user=max_devices,
    )


def _register_account(client: TestClient, label: str) -> dict[str, object]:
    response = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": f"push-{label}-{uuid.uuid4().hex}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": f"Push {label}",
            "locale": "en",
            "client_type": "ios",
        },
    )
    assert response.status_code == 201
    return response.json()


def _headers(auth: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _put_device(
    client: TestClient,
    auth: dict[str, object],
    installation_id: str,
    token: str,
    *,
    locale: str | None = "en",
):
    payload: dict[str, object] = {
        "token": token,
        "environment": "sandbox",
    }
    if locale is not None:
        payload["locale"] = locale
    return client.put(
        f"{API_V1}/push/devices/{installation_id}",
        headers=_headers(auth),
        json=payload,
    )


def test_push_registration_is_fail_closed_but_delete_remains_available(
    client: TestClient,
) -> None:
    auth = _register_account(client, "disabled")
    installation_id = f"install-{uuid.uuid4()}"

    rejected = _put_device(client, auth, installation_id, "ab" * 16)
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "push_not_configured"

    deleted = client.delete(
        f"{API_V1}/push/devices/{installation_id}",
        headers=_headers(auth),
    )
    assert deleted.status_code == 204
    assert deleted.content == b""


def test_malformed_push_token_is_redacted_from_validation_response(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings()
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    auth = _register_account(client, "token-redaction")
    raw_token = "a1" * 257

    response = _put_device(
        client,
        auth,
        f"install-{uuid.uuid4()}",
        raw_token,
    )

    assert response.status_code == 422
    serialized = json.dumps(response.json())
    assert raw_token not in serialized
    assert "[redacted]" in serialized


def test_register_rotate_unregister_and_reject_qr_guest_binding(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings()
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    auth = _register_account(client, "lifecycle")
    installation_id = f"install-{uuid.uuid4()}"

    created = _put_device(client, auth, installation_id, "AB" * 7, locale="ko_KR")
    assert created.status_code == 200
    assert created.json() == {
        "installation_id": installation_id,
        "platform": "ios",
        "environment": "sandbox",
        "topic": "im.fofu.fofu",
        "locale": "ko-KR",
        "is_active": True,
        "last_registered_at": created.json()["last_registered_at"],
    }
    assert "token" not in created.json()

    rotated = _put_device(client, auth, installation_id, "cd" * 19)
    assert rotated.status_code == 200
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(models.PushDevice).where(
                    models.PushDevice.installation_id == installation_id
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].device_token == "cd" * 19

    odd_token = _put_device(client, auth, installation_id, "abc")
    assert odd_token.status_code == 422
    mismatch = client.put(
        f"{API_V1}/push/devices/{installation_id}",
        headers=_headers(auth),
        json={"token": "ef" * 8, "environment": "production"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "push_environment_mismatch"

    deleted = client.delete(
        f"{API_V1}/push/devices/{installation_id}", headers=_headers(auth)
    )
    repeated = client.delete(
        f"{API_V1}/push/devices/{installation_id}", headers=_headers(auth)
    )
    assert deleted.status_code == repeated.status_code == 204
    with SessionLocal() as db:
        device = db.scalar(
            select(models.PushDevice).where(
                models.PushDevice.installation_id == installation_id
            )
        )
        assert device is not None
        assert device.is_active is False
        assert device.invalidated_reason == "user_unregistered"

    qr_response = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={
            "code": DEMO_QR_CODE,
            "locale": "en",
            "client_type": "ios",
            "device_id": f"qr-{uuid.uuid4()}",
        },
    )
    assert qr_response.status_code == 201
    qr_auth = qr_response.json()
    qr_installation = f"install-{uuid.uuid4()}"
    qr_registered = _put_device(client, qr_auth, qr_installation, "12" * 11)
    assert qr_registered.status_code == 403
    assert qr_registered.json()["error"]["code"] == "full_ios_push_session_required"


def test_device_cap_allows_idempotent_updates_and_ignores_stale_sessions(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings(max_devices=2)
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    email = f"push-cap-{uuid.uuid4().hex}@example.com"
    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": "Push Cap",
            "client_type": "ios",
        },
    ).json()

    sessions = [registered]
    for _ in range(2):
        response = client.post(
            f"{API_V1}/auth/login",
            json={"email": email, "password": "correct-horse-battery-staple", "client_type": "ios"},
        )
        assert response.status_code == 200
        sessions.append(response.json())

    installations = [f"install-{uuid.uuid4()}" for _ in range(3)]
    assert _put_device(client, sessions[0], installations[0], "01" * 8).status_code == 200
    assert _put_device(client, sessions[1], installations[1], "02" * 9).status_code == 200
    assert _put_device(client, sessions[0], installations[0], "03" * 10).status_code == 200

    limited = _put_device(client, sessions[2], installations[2], "04" * 11)
    assert limited.status_code == 409
    assert limited.json()["error"]["code"] == "push_device_limit_reached"

    first_session_id = sessions[0]["access_token"]
    # Simulate a legacy/external revocation that predates push-device cleanup.
    with SessionLocal() as db:
        first_device = db.scalar(
            select(models.PushDevice).where(
                models.PushDevice.installation_id == installations[0]
            )
        )
        assert first_device is not None
        auth_session = db.get(models.AuthSession, first_device.auth_session_id)
        assert auth_session is not None
        auth_session.revoked_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    del first_session_id

    accepted = _put_device(client, sessions[2], installations[2], "04" * 11)
    assert accepted.status_code == 200
    with SessionLocal() as db:
        stale = db.scalar(
            select(models.PushDevice).where(
                models.PushDevice.installation_id == installations[0]
            )
        )
        assert stale is not None and stale.is_active is False


def test_logout_deactivates_the_bound_device_and_pending_delivery(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings()
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    auth = _register_account(client, "logout")
    installation_id = f"install-{uuid.uuid4()}"
    assert _put_device(client, auth, installation_id, "55" * 12).status_code == 200

    with SessionLocal() as db:
        device = db.scalar(
            select(models.PushDevice).where(
                models.PushDevice.installation_id == installation_id
            )
        )
        assert device is not None
        delivery = models.PushDelivery(
            event_key=f"message:{uuid.uuid4()}",
            device_id=device.id,
            recipient_user_id=device.user_id,
            notification_type="message",
            title="FOFU",
            body="You have a new message.",
            payload={},
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id

    logged_out = client.post(f"{API_V1}/auth/logout", headers=_headers(auth))
    assert logged_out.status_code == 200
    assert logged_out.json()["revoked"] is True
    with SessionLocal() as db:
        device = db.scalar(
            select(models.PushDevice).where(
                models.PushDevice.installation_id == installation_id
            )
        )
        delivery = db.get(models.PushDelivery, delivery_id)
        assert device is not None and device.is_active is False
        assert device.invalidated_reason == "session_revoked"
        assert delivery is not None and delivery.status == "cancelled"
        assert delivery.last_error_code == "session_revoked"


def test_push_registration_rate_limit_returns_stable_error(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings()
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    monkeypatch.setattr(push_service.push_registration_limiter, "requests", 2)
    auth = _register_account(client, "rate-limit")
    installation_id = f"install-{uuid.uuid4()}"

    assert _put_device(client, auth, installation_id, "61" * 8).status_code == 200
    assert _put_device(client, auth, installation_id, "62" * 8).status_code == 200
    limited = _put_device(client, auth, installation_id, "63" * 8)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert limited.headers["retry-after"] == "60"


def test_message_commit_enqueues_one_private_idempotent_delivery(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = _push_settings()
    monkeypatch.setattr(push_service, "get_settings", lambda: settings)
    sender = _register_account(client, "sender")
    recipient = _register_account(client, "recipient")
    sender_installation = f"install-{uuid.uuid4()}"
    recipient_installation = f"install-{uuid.uuid4()}"
    assert _put_device(client, sender, sender_installation, "11" * 10).status_code == 200
    assert _put_device(client, recipient, recipient_installation, "22" * 10).status_code == 200

    conversation = client.post(
        f"{API_V1}/conversations",
        headers=_headers(sender),
        json={
            "kind": "direct",
            "participant_user_ids": [recipient["user"]["id"]],
        },
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    secret_body = f"allergy-secret-{uuid.uuid4().hex}"
    client_message_id = f"push-message-{uuid.uuid4().hex}"
    request = {
        "body": secret_body,
        "kind": "text",
        "client_message_id": client_message_id,
    }
    sent = client.post(
        f"{API_V1}/conversations/{conversation_id}/messages",
        headers=_headers(sender),
        json=request,
    )
    replayed = client.post(
        f"{API_V1}/conversations/{conversation_id}/messages",
        headers=_headers(sender),
        json=request,
    )
    assert sent.status_code == replayed.status_code == 201
    assert replayed.json()["idempotency_replayed"] is True

    with SessionLocal() as db:
        deliveries = list(
            db.scalars(
                select(models.PushDelivery).where(
                    models.PushDelivery.event_key == f"message:{sent.json()['id']}"
                )
            ).all()
        )
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.recipient_user_id == recipient["user"]["id"]
        assert delivery.title == "FOFU"
        assert delivery.body == "You have a new message."
        assert delivery.payload == {
            "fofu_type": "message",
            "conversation_id": conversation_id,
            "message_id": sent.json()["id"],
        }
        serialized = json.dumps(
            {"title": delivery.title, "body": delivery.body, "payload": delivery.payload}
        )
        assert secret_body not in serialized
        assert sender["user"]["display_name"] not in serialized
        assert db.scalar(
            select(func.count(models.PushDelivery.id)).where(
                models.PushDelivery.event_key == delivery.event_key
            )
        ) == 1
