from __future__ import annotations

import uuid

import pytest
from conftest import API_V1, DEMO_QR_CODE
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, rate_limit
from app.database import SessionLocal
from app.google_identity import GoogleIdentityError, VerifiedGoogleIdentity
from app.services import auth as auth_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _stub_google(
    monkeypatch,
    identities: dict[str, VerifiedGoogleIdentity],
) -> None:
    def fake_verify(token: str, *, settings=None) -> VerifiedGoogleIdentity:  # noqa: ANN001
        try:
            return identities[token]
        except KeyError as exc:
            raise GoogleIdentityError(
                "invalid_google_token",
                "The Google identity token is invalid or expired.",
            ) from exc

    monkeypatch.setattr(auth_service, "verify_google_id_token", fake_verify)


def test_google_login_creates_identity_and_reuses_subject(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    subject = f"google-subject-{suffix}"
    first_email = f"google-{suffix}@example.com"
    updated_email = f"google-updated-{suffix}@example.com"
    _stub_google(
        monkeypatch,
        {
            "first-token": VerifiedGoogleIdentity(subject, first_email, "Google Diner"),
            "second-token": VerifiedGoogleIdentity(subject, updated_email, "Changed Name"),
        },
    )

    created = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "first-token", "client_type": "ios", "locale": "ko"},
    )
    assert created.status_code == 200
    first = created.json()
    assert first["user"]["email"] == first_email
    assert first["user"]["display_name"] == "Google Diner"
    assert first["user"]["is_guest"] is False
    assert first["user"]["roles"] == ["customer"]
    assert first["refresh_token"]

    repeated = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "second-token", "client_type": "ios", "locale": "en"},
    )
    assert repeated.status_code == 200
    second = repeated.json()
    assert second["user"]["id"] == first["user"]["id"]
    assert second["user"]["email"] == updated_email
    assert second["user"]["display_name"] == "Google Diner"

    with SessionLocal() as db:
        stored = db.scalars(
            select(models.AuthIdentity).where(
                models.AuthIdentity.provider == "google",
                models.AuthIdentity.subject == subject,
            )
        ).all()
        assert len(stored) == 1
        assert stored[0].user_id == first["user"]["id"]
        assert stored[0].email == updated_email


def test_google_login_upgrades_guest_and_preserves_state(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    email = f"guest-google-{suffix}@example.com"
    _stub_google(
        monkeypatch,
        {
            "guest-upgrade-token": VerifiedGoogleIdentity(
                f"guest-subject-{suffix}",
                email,
                "Upgraded Diner",
            )
        },
    )
    guest = client.post(
        f"{API_V1}/auth/anonymous",
        json={"client_type": "ios", "locale": "en", "display_name": "Guest"},
    ).json()
    guest_headers = _bearer(guest["access_token"])
    guest_id = guest["user"]["id"]
    anonymous_refresh_token = guest["refresh_token"]

    updated_passport = client.patch(
        f"{API_V1}/me/passport",
        headers=guest_headers,
        json={"version": 1, "avoid_ingredient_codes": ["pork"]},
    )
    assert updated_passport.status_code == 200

    qr_session = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=guest_headers,
        json={"code": DEMO_QR_CODE, "client_type": "ios", "locale": "en"},
    )
    assert qr_session.status_code == 201
    qr_body = qr_session.json()
    assert qr_body["user"]["id"] == guest_id
    qr_headers = _bearer(qr_body["access_token"])

    upgraded = client.post(
        f"{API_V1}/auth/google",
        headers=qr_headers,
        json={"id_token": "guest-upgrade-token", "client_type": "ios", "locale": "fr"},
    )
    assert upgraded.status_code == 200
    body = upgraded.json()
    assert body["user"]["id"] == guest_id
    assert body["user"]["email"] == email
    assert body["user"]["is_guest"] is False
    assert body["user"]["locale"] == "fr"

    stale_guest = client.get(f"{API_V1}/auth/me", headers=guest_headers)
    assert stale_guest.status_code == 401
    assert stale_guest.json()["error"]["code"] == "invalid_session"
    stale_qr = client.get(f"{API_V1}/auth/me", headers=qr_headers)
    assert stale_qr.status_code == 401
    assert stale_qr.json()["error"]["code"] == "invalid_session"

    for stale_refresh_token in (anonymous_refresh_token, qr_body["refresh_token"]):
        stale_refresh = client.post(
            f"{API_V1}/auth/refresh",
            json={"refresh_token": stale_refresh_token},
        )
        assert stale_refresh.status_code == 401
        assert stale_refresh.json()["error"]["code"] == "invalid_refresh_token"

    preserved_passport = client.get(
        f"{API_V1}/me/passport",
        headers=_bearer(body["access_token"]),
    )
    assert preserved_passport.status_code == 200
    assert preserved_passport.json()["avoid_ingredient_codes"] == ["pork"]


def test_google_login_requires_authenticated_link_for_existing_email(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    email = f"existing-google-{suffix}@example.com"
    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "display_name": "Existing Diner",
            "locale": "en",
            "client_type": "ios",
            "device_id": "primary-before-link",
        },
    ).json()
    other_device = client.post(
        f"{API_V1}/auth/login",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "client_type": "ios",
            "device_id": "other-device",
        },
    ).json()
    qr_session = client.post(
        f"{API_V1}/guest-sessions/qr",
        headers=_bearer(registered["access_token"]),
        json={
            "code": DEMO_QR_CODE,
            "client_type": "ios",
            "device_id": "primary-qr",
            "locale": "en",
        },
    )
    assert qr_session.status_code == 201
    qr_body = qr_session.json()
    _stub_google(
        monkeypatch,
        {
            "link-token": VerifiedGoogleIdentity(
                f"existing-subject-{suffix}",
                email,
                "Google Name",
            ),
            "different-google-token": VerifiedGoogleIdentity(
                f"different-subject-{suffix}",
                f"different-google-{suffix}@example.com",
                "Different Google Account",
            ),
        },
    )

    untrusted_link = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "link-token", "client_type": "ios"},
    )
    assert untrusted_link.status_code == 409
    assert untrusted_link.json()["error"]["code"] == "google_account_link_required"

    linked = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(qr_body["access_token"]),
        json={
            "id_token": "link-token",
            "client_type": "ios",
            "device_id": "primary-linked",
            "replaced_refresh_token": registered["refresh_token"],
        },
    )
    assert linked.status_code == 200
    linked_body = linked.json()
    assert linked_body["user"]["id"] == registered["user"]["id"]
    assert linked_body["user"]["display_name"] == "Existing Diner"

    replaced_access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(registered["access_token"]),
    )
    assert replaced_access.status_code == 401
    replaced_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": registered["refresh_token"]},
    )
    assert replaced_refresh.status_code == 401
    assert replaced_refresh.json()["error"]["code"] == "invalid_refresh_token"
    replaced_qr_access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(qr_body["access_token"]),
    )
    assert replaced_qr_access.status_code == 401
    replaced_qr_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": qr_body["refresh_token"]},
    )
    assert replaced_qr_refresh.status_code == 401
    assert replaced_qr_refresh.json()["error"]["code"] == "invalid_refresh_token"
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(other_device["access_token"]),
    ).status_code == 200
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(linked_body["access_token"]),
    ).status_code == 200

    different_google = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(linked_body["access_token"]),
        json={"id_token": "different-google-token", "client_type": "ios"},
    )
    assert different_google.status_code == 409
    assert different_google.json()["error"]["code"] == "google_identity_already_linked"
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(linked_body["access_token"]),
    ).status_code == 200

    relinked = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(linked_body["access_token"]),
        json={
            "id_token": "link-token",
            "client_type": "ios",
            "device_id": "primary-relinked",
        },
    )
    assert relinked.status_code == 200
    relinked_body = relinked.json()
    assert relinked_body["user"]["id"] == registered["user"]["id"]
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(linked_body["access_token"]),
    ).status_code == 401
    replaced_link_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": linked_body["refresh_token"]},
    )
    assert replaced_link_refresh.status_code == 401
    assert replaced_link_refresh.json()["error"]["code"] == "invalid_refresh_token"
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(relinked_body["access_token"]),
    ).status_code == 200

    with SessionLocal() as db:
        active_sessions = db.scalars(
            select(models.AuthSession).where(
                models.AuthSession.user_id == registered["user"]["id"],
                models.AuthSession.revoked_at.is_(None),
            )
        ).all()
        identities = db.scalars(
            select(models.AuthIdentity).where(
                models.AuthIdentity.user_id == registered["user"]["id"]
            )
        ).all()
    assert sorted(session.device_id for session in active_sessions) == [
        "other-device",
        "primary-relinked",
    ]
    assert [identity.subject for identity in identities] == [f"existing-subject-{suffix}"]


def test_google_web_login_uses_cookie_and_keeps_admin_console_separate(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    web_identity = VerifiedGoogleIdentity(
        f"web-subject-{suffix}",
        f"web-google-{suffix}@example.com",
        "Web Diner",
    )
    admin_email = f"google-admin-{suffix}@example.com"
    admin_identity = VerifiedGoogleIdentity(
        f"admin-subject-{suffix}",
        admin_email,
        "Google Admin",
    )
    _stub_google(
        monkeypatch,
        {"web-token": web_identity, "admin-token": admin_identity},
    )

    web_login = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "web-token", "client_type": "web"},
    )
    assert web_login.status_code == 200
    assert "refresh_token" not in web_login.json()
    assert "fofu_refresh_token=" in web_login.headers["set-cookie"]
    assert "HttpOnly" in web_login.headers["set-cookie"]

    registered_admin = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": admin_email,
            "password": "correct-horse-battery-staple",
            "display_name": "Admin",
            "locale": "en",
            "client_type": "ios",
        },
    ).json()
    admin_user_id = registered_admin["user"]["id"]
    with SessionLocal() as db:
        admin_user = db.get(models.User, admin_user_id)
        assert admin_user is not None
        admin_user.roles = ["customer", "admin"]
        db.commit()

    linked_admin = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(registered_admin["access_token"]),
        json={"id_token": "admin-token", "client_type": "ios"},
    )
    assert linked_admin.status_code == 200

    rejected = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "admin-token", "client_type": "web"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "admin_login_separated"

    with SessionLocal() as db:
        web_sessions = db.scalars(
            select(models.AuthSession).where(
                models.AuthSession.user_id == admin_user_id,
                models.AuthSession.client_type == "web",
            )
        ).all()
        assert len(web_sessions) == 1
        assert web_sessions[0].revoked_at is not None


def test_google_login_keeps_password_account_canonical_email(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    password_email = f"password-{suffix}@example.com"
    google_email = f"google-profile-{suffix}@example.com"
    password = "correct-horse-battery-staple"
    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": password_email,
            "password": password,
            "display_name": "Password Diner",
            "client_type": "ios",
        },
    ).json()
    subject = f"different-email-subject-{suffix}"
    _stub_google(
        monkeypatch,
        {"different-email-token": VerifiedGoogleIdentity(subject, google_email, "Google Diner")},
    )

    linked = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(registered["access_token"]),
        json={"id_token": "different-email-token", "client_type": "ios"},
    )
    assert linked.status_code == 200
    linked_body = linked.json()
    assert linked_body["user"]["email"] == password_email

    logged_out = client.post(
        f"{API_V1}/auth/logout",
        headers=_bearer(linked_body["access_token"]),
        json={"refresh_token": linked_body["refresh_token"]},
    )
    assert logged_out.status_code == 200

    google_login = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "different-email-token", "client_type": "ios"},
    )
    assert google_login.status_code == 200
    assert google_login.json()["user"]["email"] == password_email

    password_login = client.post(
        f"{API_V1}/auth/login",
        json={"email": password_email, "password": password, "client_type": "ios"},
    )
    assert password_login.status_code == 200
    assert password_login.json()["user"]["id"] == registered["user"]["id"]

    with SessionLocal() as db:
        user = db.get(models.User, registered["user"]["id"])
        identity = db.scalar(
            select(models.AuthIdentity).where(
                models.AuthIdentity.provider == "google",
                models.AuthIdentity.subject == subject,
            )
        )
        assert user is not None
        assert identity is not None
        assert user.email == password_email
        assert identity.email == google_email


def test_google_login_never_revokes_another_users_replaced_refresh_token(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    password = "correct-horse-battery-staple"
    victim = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": f"replacement-victim-{suffix}@example.com",
            "password": password,
            "display_name": "Victim",
            "client_type": "ios",
        },
    ).json()
    actor_email = f"replacement-actor-{suffix}@example.com"
    actor = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": actor_email,
            "password": password,
            "display_name": "Actor",
            "client_type": "ios",
        },
    ).json()
    _stub_google(
        monkeypatch,
        {
            "cross-user-replacement-token": VerifiedGoogleIdentity(
                f"replacement-actor-subject-{suffix}",
                actor_email,
                "Actor",
            )
        },
    )

    linked = client.post(
        f"{API_V1}/auth/google",
        headers=_bearer(actor["access_token"]),
        json={
            "id_token": "cross-user-replacement-token",
            "client_type": "ios",
            "replaced_refresh_token": victim["refresh_token"],
        },
    )
    assert linked.status_code == 200
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(victim["access_token"]),
    ).status_code == 200
    victim_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": victim["refresh_token"]},
    )
    assert victim_refresh.status_code == 200
    assert victim_refresh.json()["user"]["id"] == victim["user"]["id"]


def test_google_link_reloads_current_user_after_verification_and_rejects_inactive_race(
    client: TestClient,
    monkeypatch,
) -> None:
    suffix = uuid.uuid4().hex
    registered = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": f"inactive-link-{suffix}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Inactive Link",
            "client_type": "ios",
        },
    )
    assert registered.status_code == 201
    user_id = registered.json()["user"]["id"]
    subject = f"inactive-link-subject-{suffix}"
    device_id = f"inactive-link-device-{suffix}"

    def deactivate_during_verification(
        _token: str,
        *,
        settings=None,  # noqa: ANN001
    ) -> VerifiedGoogleIdentity:
        with SessionLocal() as concurrent_db:
            user = concurrent_db.get(models.User, user_id)
            assert user is not None
            user.is_active = False
            concurrent_db.commit()
        return VerifiedGoogleIdentity(
            subject,
            f"inactive-google-{suffix}@example.com",
            "Inactive Google",
        )

    monkeypatch.setattr(auth_service, "verify_google_id_token", deactivate_during_verification)

    with SessionLocal() as request_db:
        stale_user = request_db.get(models.User, user_id)
        assert stale_user is not None
        assert stale_user.is_active is True
        # Simulate a request that already passed authentication before token
        # verification yields to the concurrent account deactivation.
        request_db.commit()
        with pytest.raises(auth_service.AuthServiceError) as raised:
            auth_service.login_google_user(
                request_db,
                id_token="inactive-race-token",
                locale="en",
                client_type="ios",
                device_id=device_id,
                current_user=stale_user,
            )

    assert raised.value.status_code == 401
    assert raised.value.code == "account_inactive"
    with SessionLocal() as db:
        assert db.scalar(
            select(models.AuthIdentity.id).where(models.AuthIdentity.subject == subject)
        ) is None
        assert db.scalar(
            select(models.AuthSession.id).where(models.AuthSession.device_id == device_id)
        ) is None


@pytest.mark.parametrize("failed_flush_number", [1, 2])
def test_google_login_maps_flush_integrity_errors_to_stable_conflict(
    client: TestClient,
    monkeypatch,
    failed_flush_number: int,
) -> None:
    suffix = uuid.uuid4().hex
    subject = f"forced-race-subject-{failed_flush_number}-{suffix}"
    email = f"forced-race-{failed_flush_number}-{suffix}@example.com"
    token = f"forced-race-token-{failed_flush_number}"
    _stub_google(
        monkeypatch,
        {token: VerifiedGoogleIdentity(subject, email, "Race Test")},
    )
    original_flush = Session.flush
    flush_count = 0

    def fail_selected_flush(self, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal flush_count
        flush_count += 1
        if flush_count == failed_flush_number:
            raise IntegrityError("forced Google identity race", {}, RuntimeError("forced"))
        return original_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_selected_flush)

    failed = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": token, "client_type": "ios"},
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "google_identity_conflict"
    with SessionLocal() as db:
        assert db.scalar(select(models.User.id).where(models.User.email == email)) is None
        assert db.scalar(
            select(models.AuthIdentity.id).where(models.AuthIdentity.subject == subject)
        ) is None

    retried = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": token, "client_type": "ios"},
    )
    assert retried.status_code == 200
    assert retried.json()["user"]["email"] == email


def test_google_login_maps_verification_failure_to_auth_error(
    client: TestClient,
    monkeypatch,
) -> None:
    def reject(_token: str, *, settings=None):  # noqa: ANN001, ANN202
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token is invalid or expired.",
        )

    monkeypatch.setattr(auth_service, "verify_google_id_token", reject)

    response = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "invalid-token", "client_type": "ios"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_google_token"
    assert response.headers["www-authenticate"] == "Bearer"


def test_google_login_applies_rate_limit_before_token_verification(
    client: TestClient,
    monkeypatch,
) -> None:
    verification_called = False

    def block(_key: str) -> None:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limit_exceeded", "message": "Try again later."},
            headers={"Retry-After": "60"},
        )

    def unexpected_verify(_token: str, *, settings=None):  # noqa: ANN001, ANN202
        nonlocal verification_called
        verification_called = True

    monkeypatch.setattr(rate_limit.google_auth_limiter, "check", block)
    monkeypatch.setattr(auth_service, "verify_google_id_token", unexpected_verify)

    response = client.post(
        f"{API_V1}/auth/google",
        json={"id_token": "rate-limited-token", "client_type": "ios"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert response.headers["retry-after"] == "60"
    assert verification_called is False
