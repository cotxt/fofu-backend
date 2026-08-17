from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from cachecontrol.adapter import CacheControlAdapter
from google.auth.exceptions import TransportError

from app import google_identity as google_identity_module
from app.config import Settings
from app.google_identity import (
    CachedGoogleAuthRequest,
    GoogleIdentityError,
    verify_google_id_token,
)


def _settings(*client_ids: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        google_oauth_client_ids=list(client_ids),
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": "ios-client.apps.googleusercontent.com",
        "sub": "google-subject-123",
        "email": "Diner@Example.com",
        "email_verified": True,
        "name": "  Test Diner  ",
    }
    claims.update(overrides)
    return claims


def test_google_token_verifier_returns_normalized_trusted_claims(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_verify(token, request, audience):  # noqa: ANN001, ANN202
        captured.update(token=token, request=request, audience=audience)
        return _claims()

    monkeypatch.setattr(
        "app.google_identity.google_id_token.verify_oauth2_token",
        fake_verify,
    )

    identity = verify_google_id_token(
        " signed-google-token ",
        settings=_settings(
            "ios-client.apps.googleusercontent.com",
            "web-client.apps.googleusercontent.com",
        ),
    )

    assert identity.subject == "google-subject-123"
    assert identity.email == "diner@example.com"
    assert identity.display_name == "Test Diner"
    assert captured["token"] == "signed-google-token"
    assert captured["audience"] is None
    assert captured["request"] is google_identity_module._GOOGLE_AUTH_REQUEST


def test_google_certificate_transport_reuses_cache_and_bounds_timeout() -> None:
    observed_timeouts: list[float] = []

    def fake_request(_url: str, **kwargs: Any) -> object:
        observed_timeouts.append(kwargs["timeout"])
        return object()

    transport = CachedGoogleAuthRequest(fake_request, timeout_seconds=2.0)
    transport("https://certs.example", timeout=120)
    transport("https://certs.example", timeout=0.5)

    assert observed_timeouts == [2.0, 0.5]
    cached_session = google_identity_module._GOOGLE_AUTH_REQUEST._request.session
    assert isinstance(cached_session.adapters["https://"], CacheControlAdapter)


def test_google_certificate_transport_serializes_shared_session_access() -> None:
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_request(_url: str, **_kwargs: Any) -> object:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with state_lock:
            active -= 1
        return object()

    transport = CachedGoogleAuthRequest(fake_request, timeout_seconds=1.0)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: transport("https://certs.example"), range(8)))

    assert len(results) == 8
    assert max_active == 1


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"iss": "https://issuer.example"}, "issuer"),
        ({"aud": "other-client.apps.googleusercontent.com"}, "application"),
        ({"sub": ""}, "subject"),
        ({"email_verified": False}, "verified"),
        ({"email": "not-an-email"}, "email address"),
    ],
)
def test_google_token_verifier_rejects_invalid_claims(
    monkeypatch,
    overrides: dict[str, Any],
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        "app.google_identity.google_id_token.verify_oauth2_token",
        lambda *_args, **_kwargs: _claims(**overrides),
    )

    with pytest.raises(GoogleIdentityError, match=expected_message) as raised:
        verify_google_id_token(
            "signed-google-token",
            settings=_settings("ios-client.apps.googleusercontent.com"),
        )

    assert raised.value.status_code == 401


def test_google_token_verifier_requires_configured_audience(monkeypatch) -> None:
    called = False

    def unexpected_verify(*_args, **_kwargs):  # noqa: ANN202
        nonlocal called
        called = True

    monkeypatch.setattr(
        "app.google_identity.google_id_token.verify_oauth2_token",
        unexpected_verify,
    )

    with pytest.raises(GoogleIdentityError) as raised:
        verify_google_id_token("signed-google-token", settings=_settings())

    assert raised.value.code == "google_auth_not_configured"
    assert raised.value.status_code == 503
    assert called is False


@pytest.mark.parametrize(
    ("upstream_error", "expected_code", "expected_status"),
    [
        (ValueError("bad signature or expiry"), "invalid_google_token", 401),
        (TransportError("certificate endpoint unavailable"), "google_auth_unavailable", 503),
    ],
)
def test_google_token_verifier_maps_library_failures(
    monkeypatch,
    upstream_error: Exception,
    expected_code: str,
    expected_status: int,
) -> None:
    def fail(*_args, **_kwargs):  # noqa: ANN202
        raise upstream_error

    monkeypatch.setattr(
        "app.google_identity.google_id_token.verify_oauth2_token",
        fail,
    )

    with pytest.raises(GoogleIdentityError) as raised:
        verify_google_id_token(
            "signed-google-token",
            settings=_settings("ios-client.apps.googleusercontent.com"),
        )

    assert raised.value.code == expected_code
    assert raised.value.status_code == expected_status
