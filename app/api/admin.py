from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.dependencies import AdminUser, DBSession
from app.schemas.admin import (
    AdminAuditEventListResponse,
    AdminLoginRequest,
    AdminOverviewResponse,
    AdminOwnerApplicationListResponse,
    AdminOwnerApplicationResponse,
    AdminOwnerApplicationReview,
    AdminRestaurantListResponse,
    AdminRestaurantModerationUpdate,
    AdminRestaurantResponse,
    AdminUserListResponse,
)
from app.schemas.auth import AuthResponse, LogoutResponse
from app.services import admin as admin_service
from app.services import auth as auth_service

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN_REFRESH_COOKIE_NAME = "fofu_admin_refresh_token"


def _same_origin_required(request: Request) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if not origin or origin != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_same_origin_required",
                "message": "Admin browser authentication is only available from this origin.",
            },
        )


def _set_admin_refresh_cookie(
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
        key=ADMIN_REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=aware_expiry,
        path=f"{settings.api_v1_prefix}/admin/auth",
        secure=settings.environment in {"staging", "production"},
        httponly=True,
        samesite="strict",
    )


def _clear_admin_refresh_cookie(
    response: Response, *, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    response.delete_cookie(
        ADMIN_REFRESH_COOKIE_NAME,
        path=f"{settings.api_v1_prefix}/admin/auth",
        secure=settings.environment in {"staging", "production"},
        httponly=True,
        samesite="strict",
    )


def _raise_auth_error(exc: auth_service.AuthServiceError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    ) from exc


def _revoke_invalid_admin_session(
    db: DBSession,
    credentials: auth_service.IssuedCredentials,
) -> None:
    auth_service.revoke_sessions(
        db,
        user_id=credentials.user.id,
        session_id=credentials.session.id,
    )


@router.post("/auth/login", response_model=AuthResponse, response_model_exclude_none=True)
def admin_login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    db: DBSession,
) -> AuthResponse:
    _same_origin_required(request)
    try:
        credentials = auth_service.login_user(
            db,
            email=str(payload.email),
            password=payload.password,
            client_type="web",
            device_id="fofu-admin-web",
            scope="admin",
        )
    except auth_service.AuthServiceError as exc:
        _raise_auth_error(exc)
    if "admin" not in (credentials.user.roles or []):
        _revoke_invalid_admin_session(db, credentials)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_access_required",
                "message": "Administrator access is required.",
            },
        )
    _set_admin_refresh_cookie(response, credentials.refresh_token, credentials.session.expires_at)
    return auth_service.auth_response(credentials, expose_refresh_token=False)


@router.post("/auth/refresh", response_model=AuthResponse, response_model_exclude_none=True)
def admin_refresh(
    request: Request,
    response: Response,
    db: DBSession,
    refresh_token: str | None = Cookie(default=None, alias=ADMIN_REFRESH_COOKIE_NAME),
) -> AuthResponse:
    _same_origin_required(request)
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "refresh_token_required", "message": "A refresh token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        credentials = auth_service.rotate_refresh_token(db, refresh_token)
    except auth_service.AuthServiceError as exc:
        _clear_admin_refresh_cookie(response)
        _raise_auth_error(exc)
    if credentials.session.scope != "admin" or "admin" not in (credentials.user.roles or []):
        _revoke_invalid_admin_session(db, credentials)
        _clear_admin_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_access_required",
                "message": "Administrator access is required.",
            },
        )
    _set_admin_refresh_cookie(response, credentials.refresh_token, credentials.session.expires_at)
    return auth_service.auth_response(credentials, expose_refresh_token=False)


@router.post("/auth/logout", response_model=LogoutResponse)
def admin_logout(
    request: Request,
    response: Response,
    db: DBSession,
    refresh_token: str | None = Cookie(default=None, alias=ADMIN_REFRESH_COOKIE_NAME),
) -> LogoutResponse:
    _same_origin_required(request)
    revoked = (
        auth_service.revoke_sessions(db, raw_refresh_token=refresh_token)
        if refresh_token
        else False
    )
    _clear_admin_refresh_cookie(response)
    return LogoutResponse(revoked=revoked)


@router.get("/overview", response_model=AdminOverviewResponse)
def get_admin_overview(db: DBSession, _: AdminUser) -> AdminOverviewResponse:
    return admin_service.get_overview(db)


@router.get("/users", response_model=AdminUserListResponse)
def get_admin_users(
    db: DBSession,
    _: AdminUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AdminUserListResponse:
    return admin_service.list_users(db, query=q, limit=limit, offset=offset)


@router.get("/restaurants", response_model=AdminRestaurantListResponse)
def get_admin_restaurants(
    db: DBSession,
    _: AdminUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AdminRestaurantListResponse:
    return admin_service.list_restaurants(db, query=q, limit=limit, offset=offset)


@router.patch(
    "/restaurants/{restaurant_id}",
    response_model=AdminRestaurantResponse,
    summary="Update restaurant moderation state",
)
def patch_admin_restaurant(
    restaurant_id: str,
    payload: AdminRestaurantModerationUpdate,
    db: DBSession,
    admin: AdminUser,
) -> AdminRestaurantResponse:
    return admin_service.update_restaurant_moderation(db, admin, restaurant_id, payload)


@router.get("/owner-applications", response_model=AdminOwnerApplicationListResponse)
def get_admin_owner_applications(
    db: DBSession,
    _: AdminUser,
    application_status: Annotated[str | None, Query(alias="status", max_length=30)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AdminOwnerApplicationListResponse:
    return admin_service.list_owner_applications(
        db,
        application_status=application_status,
        query=q,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/owner-applications/{application_id}/review",
    response_model=AdminOwnerApplicationResponse,
)
def patch_admin_owner_application(
    application_id: str,
    payload: AdminOwnerApplicationReview,
    db: DBSession,
    admin: AdminUser,
) -> AdminOwnerApplicationResponse:
    return admin_service.review_owner_application(db, admin, application_id, payload)


@router.get(
    "/owner-applications/{application_id}/license",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "application/pdf": {}}}},
)
def get_admin_owner_application_license(
    application_id: str,
    db: DBSession,
    admin: AdminUser,
) -> FileResponse:
    asset, path = admin_service.get_license_download(db, admin, application_id)
    return FileResponse(
        path,
        media_type=asset.content_type,
        filename=asset.original_filename,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/audit-events", response_model=AdminAuditEventListResponse)
def get_admin_audit_events(
    db: DBSession,
    _: AdminUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AdminAuditEventListResponse:
    return admin_service.list_audit_events(db, limit=limit, offset=offset)
