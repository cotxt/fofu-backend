from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status

from app.config import Settings, get_settings
from app.dependencies import OPTIONAL_AUTH_OPENAPI, CurrentUser, DBSession, OptionalPrincipal
from app.rate_limit import enforce_google_auth_rate_limit
from app.schemas.auth import (
    AnonymousRequest,
    AuthResponse,
    GoogleLoginRequest,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    UserSummary,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["authentication"])
REFRESH_COOKIE_NAME = "fofu_refresh_token"
GoogleAuthRateLimit = Annotated[None, Depends(enforce_google_auth_rate_limit)]


def set_refresh_cookie(
    response: Response,
    token: str,
    expires_at: datetime,
    *,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    aware_expiry = (
        expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
    )
    max_age = max(0, int((aware_expiry - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=aware_expiry,
        path=settings.api_v1_prefix,
        secure=settings.environment in {"staging", "production"},
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response, *, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=settings.api_v1_prefix,
        secure=settings.environment in {"staging", "production"},
        httponly=True,
        samesite="lax",
    )


def _raise_service_error(exc: auth_service.AuthServiceError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    ) from exc


def _respond_with_credentials(
    response: Response,
    credentials: auth_service.IssuedCredentials,
) -> AuthResponse:
    if credentials.session.client_type == "web":
        set_refresh_cookie(response, credentials.refresh_token, credentials.session.expires_at)
    return auth_service.auth_response(
        credentials,
        expose_refresh_token=credentials.session.client_type != "web",
    )


@router.post(
    "/anonymous",
    response_model=AuthResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
def anonymous(payload: AnonymousRequest, response: Response, db: DBSession) -> AuthResponse:
    credentials = auth_service.create_anonymous_user(
        db,
        locale=payload.locale,
        display_name=payload.display_name,
        client_type=payload.client_type,
        device_id=payload.device_id,
    )
    return _respond_with_credentials(response, credentials)


@router.post(
    "/register",
    response_model=AuthResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def register(
    payload: RegisterRequest,
    response: Response,
    db: DBSession,
    principal: OptionalPrincipal,
) -> AuthResponse:
    try:
        credentials = auth_service.register_user(
            db,
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
            locale=payload.locale,
            home_country_code=payload.home_country_code,
            client_type=payload.client_type,
            device_id=payload.device_id,
            # Passing any authenticated user lets the service distinguish the
            # supported guest-upgrade path from an already-registered account.
            guest_user=principal.user if principal else None,
            current_session_id=principal.session.id if principal else None,
        )
    except auth_service.AuthServiceError as exc:
        _raise_service_error(exc)
    return _respond_with_credentials(response, credentials)


@router.post("/login", response_model=AuthResponse, response_model_exclude_none=True)
def login(payload: LoginRequest, response: Response, db: DBSession) -> AuthResponse:
    try:
        credentials = auth_service.login_user(
            db,
            email=str(payload.email),
            password=payload.password,
            client_type=payload.client_type,
            device_id=payload.device_id,
        )
    except auth_service.AuthServiceError as exc:
        _raise_service_error(exc)
    if payload.client_type == "web" and "admin" in (credentials.user.roles or []):
        auth_service.revoke_sessions(
            db,
            user_id=credentials.user.id,
            session_id=credentials.session.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_login_separated",
                "message": "Administrators must sign in through the admin console.",
            },
        )
    return _respond_with_credentials(response, credentials)


@router.post(
    "/google",
    response_model=AuthResponse,
    response_model_exclude_none=True,
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def google_login(
    payload: GoogleLoginRequest,
    response: Response,
    db: DBSession,
    principal: OptionalPrincipal,
    _rate_limit: GoogleAuthRateLimit,
) -> AuthResponse:
    try:
        credentials = auth_service.login_google_user(
            db,
            id_token=payload.id_token,
            locale=payload.locale,
            client_type=payload.client_type,
            device_id=payload.device_id,
            current_user=principal.user if principal else None,
            current_session_id=principal.session.id if principal else None,
            replaced_refresh_token=payload.replaced_refresh_token,
        )
    except auth_service.AuthServiceError as exc:
        _raise_service_error(exc)
    if payload.client_type == "web" and "admin" in (credentials.user.roles or []):
        auth_service.revoke_sessions(
            db,
            user_id=credentials.user.id,
            session_id=credentials.session.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_login_separated",
                "message": "Administrators must sign in through the admin console.",
            },
        )
    return _respond_with_credentials(response, credentials)


@router.post("/refresh", response_model=AuthResponse, response_model_exclude_none=True)
def refresh(
    response: Response,
    db: DBSession,
    payload: RefreshRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> AuthResponse:
    raw_token = payload.refresh_token if payload and payload.refresh_token else cookie_token
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "refresh_token_required", "message": "A refresh token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        credentials = auth_service.rotate_refresh_token(db, raw_token)
    except auth_service.AuthServiceError as exc:
        clear_refresh_cookie(response)
        _raise_service_error(exc)
    if (
        credentials.session.client_type == "web"
        and credentials.session.scope == "full"
        and "admin" in (credentials.user.roles or [])
    ):
        auth_service.revoke_sessions(
            db,
            user_id=credentials.user.id,
            session_id=credentials.session.id,
        )
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_login_separated",
                "message": "Administrators must sign in through the admin console.",
            },
        )
    return _respond_with_credentials(response, credentials)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def logout(
    response: Response,
    db: DBSession,
    principal: OptionalPrincipal,
    payload: LogoutRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> LogoutResponse:
    requested = payload or LogoutRequest()
    raw_token = requested.refresh_token or cookie_token
    try:
        revoked = auth_service.revoke_sessions(
            db,
            user_id=principal.user.id if principal else None,
            session_id=principal.session.id if principal and not raw_token else None,
            raw_refresh_token=raw_token,
            all_sessions=requested.all_sessions,
        )
    except auth_service.AuthServiceError as exc:
        _raise_service_error(exc)
    clear_refresh_cookie(response)
    return LogoutResponse(revoked=revoked)


@router.get("/me", response_model=UserSummary)
def me(user: CurrentUser) -> UserSummary:
    return auth_service.user_summary(user)
