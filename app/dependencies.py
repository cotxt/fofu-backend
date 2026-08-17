from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.security import AccessClaims, TokenError, decode_access_token

bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
OPTIONAL_AUTH_OPENAPI: dict[str, object] = {
    "x-fofu-optional-auth": True,
}
DBSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user: models.User
    session: models.AuthSession
    claims: AccessClaims


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _is_expired(value: datetime) -> bool:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware <= datetime.now(timezone.utc)


def get_optional_principal(
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthenticatedPrincipal | None:
    if credentials is None:
        return None
    if credentials.scheme.casefold() != "bearer":
        raise _unauthorized("invalid_authorization_scheme", "Use a Bearer access token.")
    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise _unauthorized("invalid_access_token", str(exc)) from exc

    auth_session = db.get(models.AuthSession, claims.session_id)
    if (
        auth_session is None
        or auth_session.user_id != claims.user_id
        or auth_session.revoked_at is not None
        or _is_expired(auth_session.expires_at)
    ):
        raise _unauthorized("invalid_session", "The session is expired or revoked.")
    if (
        auth_session.scope != claims.scope
        or auth_session.qr_restaurant_id != claims.qr_restaurant_id
    ):
        raise _unauthorized("invalid_session_scope", "The token scope is no longer valid.")
    if auth_session.is_guest_at_issue != claims.is_guest:
        raise _unauthorized("invalid_session", "The session identity is no longer valid.")

    user = db.get(models.User, claims.user_id)
    if user is None or not user.is_active or user.is_guest != claims.is_guest:
        raise _unauthorized("invalid_user", "The user is inactive or no longer valid.")
    return AuthenticatedPrincipal(user=user, session=auth_session, claims=claims)


OptionalPrincipal = Annotated[
    AuthenticatedPrincipal | None,
    Depends(get_optional_principal),
]


def get_current_principal(principal: OptionalPrincipal) -> AuthenticatedPrincipal:
    if principal is None:
        raise _unauthorized("authentication_required", "Authentication is required.")
    return principal


CurrentPrincipal = Annotated[AuthenticatedPrincipal, Depends(get_current_principal)]


def get_optional_user(principal: OptionalPrincipal) -> models.User | None:
    return principal.user if principal is not None else None


OptionalUser = Annotated[models.User | None, Depends(get_optional_user)]


def get_current_user(principal: CurrentPrincipal) -> models.User:
    """Return the ORM user directly for a stable cross-router dependency contract."""

    return principal.user


CurrentUser = Annotated[models.User, Depends(get_current_user)]


def require_registered_user(user: CurrentUser) -> models.User:
    if user.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "registered_account_required", "message": "Create an account first."},
        )
    return user


RegisteredUser = Annotated[models.User, Depends(require_registered_user)]


def require_admin(principal: CurrentPrincipal) -> models.User:
    """Require a current full-scope session whose database user is an admin.

    Roles embedded in an access token are intentionally not used here. Reading the
    current ORM user means removing the admin role takes effect immediately, even
    when an older access token has not expired yet.
    """

    user = principal.user
    session_has_admin_scope = principal.session.scope == "admin" or (
        principal.session.scope == "full"
        and principal.session.client_type in {"ios", "android"}
    )
    if user.is_guest or not session_has_admin_scope or "admin" not in (user.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_access_required",
                "message": "Administrator access is required.",
            },
        )
    return user


AdminUser = Annotated[models.User, Depends(require_admin)]
