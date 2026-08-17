from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.config import Settings, get_settings

password_hash = PasswordHash.recommended()


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    session_id: str
    roles: list[str]
    is_guest: bool
    scope: str
    qr_restaurant_id: str | None


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    return password_hash.verify(password, encoded_hash)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def digest_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    roles: list[str],
    is_guest: bool,
    scope: str = "full",
    qr_restaurant_id: str | None = None,
    lifetime_minutes: int | None = None,
    settings: Settings | None = None,
) -> tuple[str, int]:
    settings = settings or get_settings()
    lifetime = lifetime_minutes or settings.access_token_minutes
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=lifetime)
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "roles": roles,
        "guest": is_guest,
        "scope": scope,
        "qr_restaurant_id": qr_restaurant_id,
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
    }
    encoded = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return encoded, int((expires_at - now).total_seconds())


def decode_access_token(token: str, settings: Settings | None = None) -> AccessClaims:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "sid", "type", "exp", "iat"]},
        )
    except InvalidTokenError as exc:
        raise TokenError("Invalid or expired access token") from exc

    if payload.get("type") != "access":
        raise TokenError("Unexpected token type")
    return AccessClaims(
        user_id=str(payload["sub"]),
        session_id=str(payload["sid"]),
        roles=[str(role) for role in payload.get("roles", [])],
        is_guest=bool(payload.get("guest", False)),
        scope=str(payload.get("scope", "full")),
        qr_restaurant_id=payload.get("qr_restaurant_id"),
    )
