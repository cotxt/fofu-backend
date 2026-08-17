from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests
from cachecontrol import CacheControl
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import Settings, get_settings

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_CERT_REQUEST_TIMEOUT_SECONDS = 5.0
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


class GoogleIdentityError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedGoogleIdentity:
    subject: str
    email: str
    display_name: str


class CachedGoogleAuthRequest:
    """Thread-safe, cache-aware google-auth transport with bounded I/O waits."""

    def __init__(
        self,
        request: Callable[..., Any] | None = None,
        *,
        timeout_seconds: float = GOOGLE_CERT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if request is None:
            cached_session = CacheControl(requests.Session())
            request = GoogleAuthRequest(session=cached_session)
        self._request = request
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        bounded_timeout = self._timeout_seconds
        if isinstance(timeout, (int, float)) and timeout > 0:
            bounded_timeout = min(float(timeout), self._timeout_seconds)
        if not self._lock.acquire(timeout=self._timeout_seconds):
            raise TransportError("Timed out waiting for the Google certificate cache transport.")
        try:
            return self._request(
                url,
                method=method,
                body=body,
                headers=headers,
                timeout=bounded_timeout,
                **kwargs,
            )
        finally:
            self._lock.release()


_GOOGLE_AUTH_REQUEST = CachedGoogleAuthRequest()


def _audience_matches(claim: Any, accepted_audiences: set[str]) -> bool:
    if isinstance(claim, str):
        return claim in accepted_audiences
    if isinstance(claim, (list, tuple)):
        return any(isinstance(value, str) and value in accepted_audiences for value in claim)
    return False


def _verified_email(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.casefold() == "true")


def verify_google_id_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> VerifiedGoogleIdentity:
    """Verify a Google OIDC token and return only claims trusted by account logic."""

    settings = settings or get_settings()
    accepted_audiences = set(settings.google_oauth_client_ids)
    if not accepted_audiences:
        raise GoogleIdentityError(
            "google_auth_not_configured",
            "Google sign-in is not configured on the server.",
            status_code=503,
        )

    try:
        # google-auth verifies Google's signature and the temporal claims. Audience
        # is checked explicitly below so one backend can accept configured native
        # and web client IDs without ever accepting an arbitrary audience.
        claims: Mapping[str, Any] = google_id_token.verify_oauth2_token(
            token.strip(),
            _GOOGLE_AUTH_REQUEST,
            audience=None,
        )
    except TransportError as exc:
        raise GoogleIdentityError(
            "google_auth_unavailable",
            "Google sign-in verification is temporarily unavailable.",
            status_code=503,
        ) from exc
    except (GoogleAuthError, ValueError) as exc:
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token is invalid or expired.",
        ) from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token has an invalid issuer.",
        )
    audience_claim = claims.get("aud")
    if not _audience_matches(audience_claim, accepted_audiences):
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token was not issued for this application.",
        )

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject.strip()) > 255:
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token is missing a valid subject.",
        )
    if not _verified_email(claims.get("email_verified")):
        raise GoogleIdentityError(
            "google_email_not_verified",
            "Google has not verified this account's email address.",
        )

    raw_email = claims.get("email")
    try:
        email = str(_EMAIL_ADAPTER.validate_python(raw_email)).strip().casefold()
    except ValidationError as exc:
        raise GoogleIdentityError(
            "invalid_google_token",
            "The Google identity token is missing a valid email address.",
        ) from exc

    raw_name = claims.get("name")
    display_name = raw_name.strip()[:100] if isinstance(raw_name, str) else ""
    if not display_name:
        display_name = email.split("@", 1)[0][:100] or "Google User"
    return VerifiedGoogleIdentity(
        subject=subject.strip(),
        email=email,
        display_name=display_name,
    )
