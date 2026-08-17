from __future__ import annotations

import hashlib
import io
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import models
from app.api.auth import set_refresh_cookie
from app.config import Settings, get_settings
from app.dependencies import (
    OPTIONAL_AUTH_OPENAPI,
    CurrentPrincipal,
    CurrentUser,
    DBSession,
    OptionalPrincipal,
)
from app.rate_limit import enforce_qr_rate_limit
from app.schemas.qr import (
    OwnerQRCode,
    OwnerQRCodeCreate,
    OwnerQRCodeCreated,
    OwnerQRCodeList,
    OwnerQRCodeUpdate,
    QRCodeRevoked,
    QRGuestSessionRequest,
    QRGuestSessionResponse,
    QRMenuCategory,
    QRMenuItem,
    QRResolveResponse,
    QRRestaurantContext,
    QRSessionContext,
    SessionBootstrap,
)
from app.services import auth as auth_service
from app.services import cart as cart_service
from app.services import push as push_service
from app.utils import localized, normalize_locale, privacy_hash

router = APIRouter(tags=["QR guest sessions"])
web_router = APIRouter(tags=["install-free web entry"])
QRRateLimit = Annotated[None, Depends(enforce_qr_rate_limit)]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _public_web_url(code: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.public_api_base_url}/q/{quote(code, safe='')}"


def _resolve_qr(db: DBSession, code: str) -> tuple[models.QRCode, models.Restaurant]:
    if not 16 <= len(code) <= 256:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "qr_not_found", "message": "This QR code is invalid or inactive."},
        )
    qr_code = db.scalar(select(models.QRCode).where(models.QRCode.code_hash == _digest_code(code)))
    now = datetime.now(timezone.utc)
    if (
        qr_code is None
        or not qr_code.is_active
        or (qr_code.expires_at is not None and _aware(qr_code.expires_at) <= now)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "qr_not_found", "message": "This QR code is invalid or inactive."},
        )
    restaurant = db.get(models.Restaurant, qr_code.restaurant_id)
    if restaurant is None or not restaurant.is_published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "qr_not_found", "message": "This QR code is invalid or inactive."},
        )
    return qr_code, restaurant


def _menu_item_name(item: models.MenuItem, locale: str) -> tuple[str, str | None, str, str | None]:
    translation = next((row for row in item.translations if row.locale == locale), None)
    if translation:
        return translation.name, item.name_ko, translation.description, translation.pronunciation
    return (
        localized(item.name_en, item.name_ko, locale),
        item.name_ko,
        localized(item.description_en, item.description_ko, locale),
        item.pronunciation,
    )


def _bootstrap_menu(db: DBSession, restaurant_id: str, locale: str) -> list[QRMenuCategory]:
    statement = (
        select(models.MenuCategory)
        .options(
            selectinload(models.MenuCategory.translations),
            selectinload(models.MenuCategory.items).selectinload(models.MenuItem.translations),
            selectinload(models.MenuCategory.items).selectinload(models.MenuItem.ingredient_links),
            selectinload(models.MenuCategory.items).selectinload(models.MenuItem.allergen_links),
            selectinload(models.MenuCategory.items).selectinload(models.MenuItem.dietary_claims),
        )
        .where(
            models.MenuCategory.restaurant_id == restaurant_id,
            models.MenuCategory.is_active.is_(True),
        )
        .order_by(models.MenuCategory.sort_order, models.MenuCategory.id)
    )
    categories: list[QRMenuCategory] = []
    for category in db.scalars(statement).unique().all():
        category_translation = next(
            (row for row in category.translations if row.locale == locale), None
        )
        category_name = (
            category_translation.name
            if category_translation
            else localized(category.name_en, category.name_ko, locale)
        )
        items: list[QRMenuItem] = []
        for item in sorted(category.items, key=lambda value: (value.sort_order, value.id)):
            if not item.is_available:
                continue
            name, original_name, description, pronunciation = _menu_item_name(item, locale)
            items.append(
                QRMenuItem(
                    id=item.id,
                    category_id=item.category_id,
                    slug=item.slug,
                    name=name,
                    original_name=original_name,
                    pronunciation=pronunciation,
                    description=description,
                    price=(
                        cart_service.money(item.price_amount, item.currency)
                        if item.price_amount is not None
                        else None
                    ),
                    serving_description=item.serving_description,
                    spice_level=item.spice_level,
                    badge=item.badge,
                    image_url=item.image_url,
                    is_available=item.is_available,
                    # Price is optional because this flow prepares a Korean
                    # order card; it does not execute a payment.
                    is_orderable=item.is_available,
                    orderability_reason=(
                        None
                        if item.is_available and item.price_amount is not None
                        else "unavailable"
                        if not item.is_available
                        else "price_unknown"
                    ),
                    ingredient_codes=[link.ingredient_code for link in item.ingredient_links],
                    allergen_codes=sorted({link.allergen_code for link in item.allergen_links}),
                    dietary_claims=[claim.code for claim in item.dietary_claims],
                )
            )
        categories.append(
            QRMenuCategory(
                id=category.id,
                slug=category.slug,
                name=category_name,
                sort_order=category.sort_order,
                items=items,
            )
        )
    return categories


def _restaurant_context(restaurant: models.Restaurant, locale: str) -> QRRestaurantContext:
    translation = next((row for row in restaurant.translations if row.locale == locale), None)
    return QRRestaurantContext(
        id=restaurant.id,
        slug=restaurant.slug,
        name=translation.name
        if translation
        else localized(restaurant.name_en, restaurant.name_ko, locale),
        original_name=restaurant.name_ko,
        description=(
            translation.description
            if translation
            else localized(restaurant.description_en, restaurant.description_ko, locale)
        ),
        address=translation.address
        if translation
        else localized(restaurant.address_en, restaurant.address_ko, locale),
        original_address=restaurant.address_ko,
        category=restaurant.category,
        phone=restaurant.phone,
        currency=restaurant.currency,
        timezone=restaurant.timezone_name,
        is_verified=restaurant.is_verified,
        is_open=restaurant.is_open,
        cover_image_url=restaurant.cover_image_url,
        menu_revision=restaurant.menu_revision,
    )


def _build_bootstrap(
    db: DBSession,
    *,
    user: models.User,
    auth_session: models.AuthSession,
    restaurant: models.Restaurant,
    cart: models.Cart,
    locale: str,
    qr_code: models.QRCode | None = None,
) -> SessionBootstrap:
    return SessionBootstrap(
        session=QRSessionContext(
            id=auth_session.id,
            scope=auth_session.scope,
            restaurant_id=restaurant.id,
            qr_code_id=qr_code.id if qr_code else None,
            qr_purpose=qr_code.purpose if qr_code else None,
            table_label=cart.table_label,
            allowed_serving_modes=["dine_in", "takeout"],
            expires_at=auth_session.expires_at,
        ),
        user=auth_service.user_summary(user),
        restaurant=_restaurant_context(restaurant, locale),
        menu=_bootstrap_menu(db, restaurant.id, locale),
        cart=cart_service.serialize_cart(db, cart, locale=locale),
    )


@router.get("/qr/{code}/svg")
def qr_svg(code: str, db: DBSession, _rate_limit: QRRateLimit) -> Response:
    _resolve_qr(db, code)
    image = qrcode.make(
        _public_web_url(code),
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=4,
    )
    output = io.BytesIO()
    image.save(output)
    return Response(
        content=output.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300", "Referrer-Policy": "no-referrer"},
    )


@router.get("/qr/{code}", response_model=QRResolveResponse)
def resolve_qr(code: str, db: DBSession, _rate_limit: QRRateLimit) -> QRResolveResponse:
    qr_code, restaurant = _resolve_qr(db, code)
    return QRResolveResponse(
        restaurant_id=restaurant.id,
        restaurant_slug=restaurant.slug,
        restaurant_name=restaurant.name_en,
        table_label=qr_code.table_label,
        purpose=qr_code.purpose,
        menu_revision=restaurant.menu_revision,
        web_url=_public_web_url(code),
    )


@web_router.get("/q/{code}", include_in_schema=False)
def web_entry(code: str, db: DBSession, _rate_limit: QRRateLimit) -> RedirectResponse:
    _, restaurant = _resolve_qr(db, code)
    settings = get_settings()
    # The fragment is not sent in the HTTP Referer or the web server request.
    # The web client exchanges it once, then removes it with history.replaceState().
    destination = (
        f"{settings.web_app_base_url}/r/{quote(restaurant.slug, safe='')}#qr={quote(code, safe='')}"
    )
    return RedirectResponse(
        destination,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post(
    "/guest-sessions/qr",
    response_model=QRGuestSessionResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=OPTIONAL_AUTH_OPENAPI,
)
def create_qr_guest_session(
    payload: QRGuestSessionRequest,
    request: Request,
    response: Response,
    db: DBSession,
    principal: OptionalPrincipal,
    _rate_limit: QRRateLimit,
) -> QRGuestSessionResponse:
    qr_code, restaurant = _resolve_qr(db, payload.code)
    locale = normalize_locale(payload.locale)
    if principal is None:
        user = models.User(
            display_name="Guest",
            locale=locale,
            is_guest=True,
            is_active=True,
            roles=[],
        )
        db.add(user)
        db.flush()
        db.add(models.FoodPassport(user_id=user.id))
    else:
        user = principal.user
        if user.is_guest:
            user.locale = locale
        if principal.session.scope in {"guest", "qr_guest"}:
            # A QR credential replaces the prior foreground guest credential.
            # Keep any full account session intact for the client's backup token.
            principal.session.revoked_at = datetime.now(timezone.utc)
            push_service.deactivate_session_devices(
                db,
                session_id=principal.session.id,
            )

    settings = get_settings()
    credentials = auth_service.issue_session(
        db,
        user,
        client_type=payload.client_type,
        device_id=payload.device_id,
        scope="qr_guest",
        qr_restaurant_id=restaurant.id,
        access_lifetime_minutes=settings.qr_guest_token_minutes,
        session_lifetime=timedelta(minutes=settings.qr_guest_token_minutes),
        settings=settings,
    )
    cart = cart_service.get_or_create_cart(
        db,
        user_id=user.id,
        restaurant=restaurant,
        table_label=qr_code.table_label,
        menu_revision=restaurant.menu_revision,
    )
    db.add(
        models.QRScan(
            qr_code_id=qr_code.id,
            locale=locale,
            client_type=payload.client_type,
            ip_hash=privacy_hash(
                request.client.host if request.client else None,
                settings.jwt_secret,
            ),
            user_agent=request.headers.get("user-agent", "")[:500] or None,
        )
    )
    tokens = auth_service.token_bundle(
        credentials,
        expose_refresh_token=credentials.session.client_type != "web",
    )
    bootstrap = _build_bootstrap(
        db,
        user=user,
        auth_session=credentials.session,
        restaurant=restaurant,
        cart=cart,
        locale=locale,
        qr_code=qr_code,
    )
    result = QRGuestSessionResponse(
        **tokens.model_dump(),
        user=auth_service.user_summary(user),
        bootstrap=bootstrap,
    )
    db.commit()
    if credentials.session.client_type == "web":
        set_refresh_cookie(response, credentials.refresh_token, credentials.session.expires_at)
    return result


@router.get("/sessions/current/bootstrap", response_model=SessionBootstrap)
def current_session_bootstrap(
    db: DBSession,
    principal: CurrentPrincipal,
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> SessionBootstrap:
    restaurant_id = principal.session.qr_restaurant_id
    if principal.session.scope != "qr_guest" or not restaurant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "qr_session_required",
                "message": "This endpoint requires a QR-scoped guest session.",
            },
        )
    restaurant = db.get(models.Restaurant, restaurant_id)
    if restaurant is None or not restaurant.is_published:
        raise HTTPException(status_code=404, detail={"code": "restaurant_not_found"})
    try:
        cart = cart_service.get_current_cart(
            db,
            user_id=principal.user.id,
            restaurant_id=restaurant.id,
        )
        cart_changed = False
        if cart.menu_revision != restaurant.menu_revision:
            cart = cart_service.get_or_create_cart(
                db,
                user_id=principal.user.id,
                restaurant=restaurant,
                table_label=cart.table_label,
                menu_revision=restaurant.menu_revision,
            )
            cart_changed = True
        result = _build_bootstrap(
            db,
            user=principal.user,
            auth_session=principal.session,
            restaurant=restaurant,
            cart=cart,
            locale=normalize_locale(locale or principal.user.locale),
        )
        if cart_changed:
            db.commit()
        return result
    except cart_service.CartServiceError as exc:
        detail: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.details:
            detail["details"] = exc.details
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _require_restaurant_manager(
    db: DBSession,
    user: models.User,
    restaurant_id: str,
) -> models.Restaurant:
    restaurant = db.get(models.Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail={"code": "restaurant_not_found"})
    if "admin" in (user.roles or []):
        return restaurant
    membership = db.scalar(
        select(models.RestaurantMembership).where(
            models.RestaurantMembership.restaurant_id == restaurant_id,
            models.RestaurantMembership.user_id == user.id,
            models.RestaurantMembership.status == "active",
        )
    )
    if (
        membership is None
        or membership.role not in {"owner", "manager"}
        or (membership.role == "owner" and restaurant.owner_user_id != user.id)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "restaurant_manager_required"},
        )
    return restaurant


def _owner_qr(qr_code: models.QRCode) -> OwnerQRCode:
    return OwnerQRCode(
        id=qr_code.id,
        public_hint=qr_code.public_hint,
        restaurant_id=qr_code.restaurant_id,
        label=qr_code.label,
        table_label=qr_code.table_label,
        purpose=qr_code.purpose,
        menu_revision=qr_code.menu_revision,
        is_active=qr_code.is_active,
        expires_at=qr_code.expires_at,
        created_at=qr_code.created_at,
        updated_at=qr_code.updated_at,
    )


@router.post(
    "/owner/restaurants/{restaurant_id}/qr-codes",
    response_model=OwnerQRCodeCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_owner_qr_code(
    restaurant_id: str,
    payload: OwnerQRCodeCreate,
    db: DBSession,
    user: CurrentUser,
) -> OwnerQRCodeCreated:
    restaurant = _require_restaurant_manager(db, user, restaurant_id)
    code = secrets.token_urlsafe(32)
    qr_code = models.QRCode(
        code_hash=_digest_code(code),
        public_hint=code[:8],
        restaurant_id=restaurant.id,
        label=payload.label,
        table_label=payload.table_label,
        purpose=payload.purpose,
        menu_revision=restaurant.menu_revision,
        is_active=True,
        expires_at=payload.expires_at,
    )
    db.add(qr_code)
    db.commit()
    base = _owner_qr(qr_code).model_dump()
    settings = get_settings()
    return OwnerQRCodeCreated(
        **base,
        code=code,
        web_url=_public_web_url(code, settings),
        svg_url=(
            f"{settings.public_api_base_url}{settings.api_v1_prefix}/qr/{quote(code, safe='')}/svg"
        ),
    )


@router.get(
    "/owner/restaurants/{restaurant_id}/qr-codes",
    response_model=OwnerQRCodeList,
)
def list_owner_qr_codes(
    restaurant_id: str,
    db: DBSession,
    user: CurrentUser,
) -> OwnerQRCodeList:
    _require_restaurant_manager(db, user, restaurant_id)
    rows = db.scalars(
        select(models.QRCode)
        .where(models.QRCode.restaurant_id == restaurant_id)
        .order_by(models.QRCode.created_at.desc())
    ).all()
    return OwnerQRCodeList(items=[_owner_qr(row) for row in rows])


@router.patch(
    "/owner/restaurants/{restaurant_id}/qr-codes/{qr_code_id}",
    response_model=OwnerQRCode,
)
def update_owner_qr_code(
    restaurant_id: str,
    qr_code_id: str,
    payload: OwnerQRCodeUpdate,
    db: DBSession,
    user: CurrentUser,
) -> OwnerQRCode:
    restaurant = _require_restaurant_manager(db, user, restaurant_id)
    qr_code = db.get(models.QRCode, qr_code_id)
    if qr_code is None or qr_code.restaurant_id != restaurant.id:
        raise HTTPException(status_code=404, detail={"code": "qr_code_not_found"})
    updates = payload.model_dump(exclude_unset=True)
    requested_revision = updates.get("menu_revision")
    if requested_revision is not None and requested_revision != restaurant.menu_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "menu_revision_conflict",
                "message": "QR codes can only be synchronized to the current menu revision.",
                "details": {"current_menu_revision": restaurant.menu_revision},
            },
        )
    resulting_purpose = updates.get("purpose", qr_code.purpose)
    resulting_table = updates.get("table_label", qr_code.table_label)
    if resulting_purpose == "table_entry" and not resulting_table:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "table_label_required"},
        )
    for field, value in updates.items():
        setattr(qr_code, field, value)
    db.commit()
    return _owner_qr(qr_code)


@router.delete(
    "/owner/restaurants/{restaurant_id}/qr-codes/{qr_code_id}",
    response_model=QRCodeRevoked,
)
def revoke_owner_qr_code(
    restaurant_id: str,
    qr_code_id: str,
    db: DBSession,
    user: CurrentUser,
) -> QRCodeRevoked:
    _require_restaurant_manager(db, user, restaurant_id)
    qr_code = db.get(models.QRCode, qr_code_id)
    if qr_code is None or qr_code.restaurant_id != restaurant_id:
        raise HTTPException(status_code=404, detail={"code": "qr_code_not_found"})
    qr_code.is_active = False
    db.commit()
    return QRCodeRevoked(id=qr_code.id, revoked=True)
