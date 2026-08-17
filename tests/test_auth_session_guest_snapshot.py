from __future__ import annotations

import uuid

import pytest
from conftest import API_V1
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.database import SessionLocal
from app.google_identity import VerifiedGoogleIdentity
from app.security import digest_refresh_token
from app.services import auth as auth_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_guest(client: TestClient) -> dict[str, object]:
    response = client.post(
        f"{API_V1}/auth/anonymous",
        json={"client_type": "ios", "locale": "en", "display_name": "Race Guest"},
    )
    assert response.status_code == 201
    return response.json()


def _stub_google(
    monkeypatch: pytest.MonkeyPatch,
    identities: dict[str, VerifiedGoogleIdentity],
) -> None:
    def fake_verify(token: str, *, settings=None) -> VerifiedGoogleIdentity:  # noqa: ANN001
        return identities[token]

    monkeypatch.setattr(auth_service, "verify_google_id_token", fake_verify)


def _google_upgrade(
    db,
    user: models.User,
    *,
    token: str,
) -> auth_service.IssuedCredentials:
    return auth_service.login_google_user(
        db,
        id_token=token,
        locale="en",
        client_type="ios",
        device_id="google-upgrade",
        current_user=user,
    )


def _password_upgrade(
    db,
    user: models.User,
    *,
    email: str,
) -> auth_service.IssuedCredentials:
    return auth_service.register_user(
        db,
        email=email,
        password="correct-horse-battery-staple",
        display_name="Password Winner",
        locale="en",
        home_country_code=None,
        client_type="ios",
        device_id="password-upgrade",
        guest_user=user,
    )


@pytest.mark.parametrize("upgrade_kind", ["google", "register"])
def test_late_stale_guest_qr_session_cannot_refresh_after_upgrade(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    upgrade_kind: str,
) -> None:
    suffix = uuid.uuid4().hex
    guest = _create_guest(client)
    user_id = guest["user"]["id"]
    google_token = f"late-qr-google-{suffix}"
    _stub_google(
        monkeypatch,
        {
            google_token: VerifiedGoogleIdentity(
                f"late-qr-subject-{suffix}",
                f"late-qr-google-{suffix}@example.com",
                "Google Winner",
            )
        },
    )

    with SessionLocal() as winner_db, SessionLocal() as stale_qr_db:
        winner_user = winner_db.get(models.User, user_id)
        stale_user = stale_qr_db.get(models.User, user_id)
        restaurant_id = stale_qr_db.scalar(select(models.Restaurant.id).limit(1))
        assert winner_user is not None
        assert stale_user is not None
        assert restaurant_id is not None
        # Both requests have crossed the authentication boundary. End only their
        # read transactions while retaining independent, issue-time guest objects.
        winner_db.commit()
        stale_qr_db.commit()

        if upgrade_kind == "google":
            winner = _google_upgrade(winner_db, winner_user, token=google_token)
        else:
            winner = _password_upgrade(
                winner_db,
                winner_user,
                email=f"late-qr-register-{suffix}@example.com",
            )

        assert stale_user.is_guest is True
        late_qr = auth_service.issue_session(
            stale_qr_db,
            stale_user,
            client_type="ios",
            device_id="late-stale-qr",
            scope="qr_guest",
            qr_restaurant_id=restaurant_id,
        )
        stale_qr_db.commit()
        late_session_id = late_qr.session.id
        assert late_qr.session.revoked_at is None
        assert late_qr.session.is_guest_at_issue is True
        with pytest.raises(auth_service.AuthServiceError) as stale_refresh_error:
            auth_service.rotate_refresh_token(stale_qr_db, late_qr.refresh_token)
        assert stale_refresh_error.value.code == "invalid_refresh_token"

    late_access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(late_qr.access_token),
    )
    assert late_access.status_code == 401
    assert late_access.json()["error"]["code"] == "invalid_user"

    late_refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": late_qr.refresh_token},
    )
    assert late_refresh.status_code == 401
    assert late_refresh.json()["error"]["code"] == "invalid_refresh_token"
    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(winner.access_token),
    ).status_code == 200

    with SessionLocal() as db:
        stored_late_session = db.get(models.AuthSession, late_session_id)
        assert stored_late_session is not None
        assert stored_late_session.revoked_at is None


def test_access_and_refresh_reject_session_guest_snapshot_mismatch(
    client: TestClient,
) -> None:
    guest = _create_guest(client)
    with SessionLocal() as db:
        session = db.scalar(
            select(models.AuthSession).where(
                models.AuthSession.refresh_token_hash
                == digest_refresh_token(guest["refresh_token"])
            )
        )
        assert session is not None
        assert session.is_guest_at_issue is True
        session.is_guest_at_issue = False
        db.commit()

    access = client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(guest["access_token"]),
    )
    assert access.status_code == 401
    assert access.json()["error"]["code"] == "invalid_session"

    refresh = client.post(
        f"{API_V1}/auth/refresh",
        json={"refresh_token": guest["refresh_token"]},
    )
    assert refresh.status_code == 401
    assert refresh.json()["error"]["code"] == "invalid_refresh_token"


@pytest.mark.parametrize("winner_kind", ["google", "register"])
def test_stale_google_vs_register_upgrade_has_one_atomic_winner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    winner_kind: str,
) -> None:
    suffix = uuid.uuid4().hex
    guest = _create_guest(client)
    user_id = guest["user"]["id"]
    google_email = f"upgrade-race-google-{suffix}@example.com"
    password_email = f"upgrade-race-password-{suffix}@example.com"
    google_token = f"upgrade-race-google-{suffix}"
    google_subject = f"upgrade-race-subject-{suffix}"
    _stub_google(
        monkeypatch,
        {
            google_token: VerifiedGoogleIdentity(
                google_subject,
                google_email,
                "Google Winner",
            )
        },
    )

    with SessionLocal() as winner_db, SessionLocal() as loser_db:
        winner_user = winner_db.get(models.User, user_id)
        loser_user = loser_db.get(models.User, user_id)
        assert winner_user is not None
        assert loser_user is not None
        winner_db.commit()
        loser_db.commit()

        if winner_kind == "google":
            winner = _google_upgrade(winner_db, winner_user, token=google_token)
            with pytest.raises(auth_service.AuthServiceError) as raised:
                _password_upgrade(loser_db, loser_user, email=password_email)
            expected_email = google_email
            expected_subject = google_subject
        else:
            winner = _password_upgrade(winner_db, winner_user, email=password_email)
            with pytest.raises(auth_service.AuthServiceError) as raised:
                _google_upgrade(loser_db, loser_user, token=google_token)
            expected_email = password_email
            expected_subject = None

        assert raised.value.status_code == 409
        assert raised.value.code == "guest_upgrade_conflict"

    with SessionLocal() as db:
        stored_user = db.get(models.User, user_id)
        identities = db.scalars(
            select(models.AuthIdentity).where(models.AuthIdentity.user_id == user_id)
        ).all()
        active_sessions = db.scalars(
            select(models.AuthSession).where(
                models.AuthSession.user_id == user_id,
                models.AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert stored_user is not None
        assert stored_user.email == expected_email
        assert stored_user.is_guest is False
        assert [identity.subject for identity in identities] == (
            [] if expected_subject is None else [expected_subject]
        )
        assert [session.id for session in active_sessions] == [winner.session.id]

    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(winner.access_token),
    ).status_code == 200


def test_two_stale_google_upgrades_cannot_replace_the_winning_identity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex
    guest = _create_guest(client)
    user_id = guest["user"]["id"]
    winner_email = f"google-race-winner-{suffix}@example.com"
    loser_email = f"google-race-loser-{suffix}@example.com"
    _stub_google(
        monkeypatch,
        {
            "winner-token": VerifiedGoogleIdentity(
                f"winner-subject-{suffix}", winner_email, "Winner"
            ),
            "loser-token": VerifiedGoogleIdentity(
                f"loser-subject-{suffix}", loser_email, "Loser"
            ),
        },
    )

    with SessionLocal() as winner_db, SessionLocal() as loser_db:
        winner_user = winner_db.get(models.User, user_id)
        loser_user = loser_db.get(models.User, user_id)
        assert winner_user is not None
        assert loser_user is not None
        winner_db.commit()
        loser_db.commit()

        winner = _google_upgrade(winner_db, winner_user, token="winner-token")
        with pytest.raises(auth_service.AuthServiceError) as raised:
            _google_upgrade(loser_db, loser_user, token="loser-token")
        assert raised.value.status_code == 409
        assert raised.value.code == "guest_upgrade_conflict"

    with SessionLocal() as db:
        stored_user = db.get(models.User, user_id)
        identities = db.scalars(
            select(models.AuthIdentity).where(models.AuthIdentity.user_id == user_id)
        ).all()
        active_sessions = db.scalars(
            select(models.AuthSession).where(
                models.AuthSession.user_id == user_id,
                models.AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert stored_user is not None
        assert stored_user.email == winner_email
        assert [identity.subject for identity in identities] == [f"winner-subject-{suffix}"]
        assert [session.id for session in active_sessions] == [winner.session.id]


def test_existing_google_identity_reloads_same_users_stale_guest_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex
    guest = _create_guest(client)
    user_id = guest["user"]["id"]
    token = f"same-google-{suffix}"
    _stub_google(
        monkeypatch,
        {
            token: VerifiedGoogleIdentity(
                f"same-google-subject-{suffix}",
                f"same-google-{suffix}@example.com",
                "Same Google",
            )
        },
    )

    with SessionLocal() as winner_db, SessionLocal() as stale_db:
        winner_user = winner_db.get(models.User, user_id)
        stale_user = stale_db.get(models.User, user_id)
        assert winner_user is not None
        assert stale_user is not None
        winner_db.commit()
        stale_db.commit()

        _google_upgrade(winner_db, winner_user, token=token)
        repeated = _google_upgrade(stale_db, stale_user, token=token)
        assert repeated.user.is_guest is False
        assert repeated.session.is_guest_at_issue is False

    assert client.get(
        f"{API_V1}/auth/me",
        headers=_bearer(repeated.access_token),
    ).status_code == 200
