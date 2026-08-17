from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.dependencies import CurrentPrincipal, DBSession
from app.schemas.cart import (
    Cart,
    CartItemUpsert,
    CartSettingsUpdate,
    OrderCard,
    OrderCardPrepareRequest,
)
from app.services import cart as cart_service
from app.utils import normalize_locale

router = APIRouter(prefix="/cart", tags=["current cart"])


def _raise_service_error(exc: cart_service.CartServiceError) -> None:
    detail: dict[str, object] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _current_cart(db: DBSession, principal: CurrentPrincipal):
    restaurant_id = principal.session.qr_restaurant_id
    if principal.session.scope != "qr_guest" or not restaurant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "qr_session_required",
                "message": "This endpoint requires a QR-scoped guest session.",
            },
        )
    try:
        return cart_service.get_current_cart(
            db,
            user_id=principal.user.id,
            restaurant_id=restaurant_id,
        )
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


def _locale(value: str | None, principal: CurrentPrincipal) -> str:
    return normalize_locale(value or principal.user.locale)


@router.get("", response_model=Cart)
def get_cart(
    db: DBSession,
    principal: CurrentPrincipal,
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> Cart:
    cart = _current_cart(db, principal)
    try:
        return cart_service.serialize_cart(db, cart, locale=_locale(locale, principal))
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


@router.put("/items/{menu_item_id}", response_model=Cart)
def put_item(
    menu_item_id: str,
    payload: CartItemUpsert,
    db: DBSession,
    principal: CurrentPrincipal,
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> Cart:
    cart = _current_cart(db, principal)
    try:
        updated = cart_service.upsert_item(db, cart, menu_item_id, payload)
        return cart_service.serialize_cart(db, updated, locale=_locale(locale, principal))
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


@router.delete("/items/{menu_item_id}", response_model=Cart)
def delete_item(
    menu_item_id: str,
    db: DBSession,
    principal: CurrentPrincipal,
    expected_version: int | None = Query(default=None, ge=1),
    expected_menu_revision: int | None = Query(default=None, ge=1),
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> Cart:
    cart = _current_cart(db, principal)
    try:
        updated = cart_service.delete_item(
            db,
            cart,
            menu_item_id,
            expected_version=expected_version,
            expected_menu_revision=expected_menu_revision,
        )
        return cart_service.serialize_cart(db, updated, locale=_locale(locale, principal))
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


@router.patch("", response_model=Cart)
def patch_cart(
    payload: CartSettingsUpdate,
    db: DBSession,
    principal: CurrentPrincipal,
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> Cart:
    cart = _current_cart(db, principal)
    try:
        updated = cart_service.update_settings(
            db,
            cart,
            serving_mode=payload.serving_mode or cart.serving_mode,
            expected_version=payload.expected_version,
            expected_menu_revision=payload.expected_menu_revision,
        )
        return cart_service.serialize_cart(db, updated, locale=_locale(locale, principal))
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


@router.get("/order-card", response_model=OrderCard)
def preview_order_card(
    db: DBSession,
    principal: CurrentPrincipal,
    expected_version: int | None = Query(default=None, ge=1),
    expected_menu_revision: int | None = Query(default=None, ge=1),
    locale: str | None = Query(default=None, min_length=2, max_length=35),
) -> OrderCard:
    cart = _current_cart(db, principal)
    try:
        return cart_service.build_order_card(
            db,
            cart,
            locale=_locale(locale, principal),
            expected_version=expected_version,
            expected_menu_revision=expected_menu_revision,
        )
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)


@router.post("/order-card", response_model=OrderCard, status_code=status.HTTP_201_CREATED)
def prepare_order_card(
    payload: OrderCardPrepareRequest,
    db: DBSession,
    principal: CurrentPrincipal,
) -> OrderCard:
    cart = _current_cart(db, principal)
    try:
        return cart_service.prepare_order_card(
            db,
            cart,
            locale=_locale(payload.locale, principal),
            idempotency_key=payload.idempotency_key,
            expected_version=payload.expected_version,
            expected_menu_revision=payload.expected_menu_revision,
        )
    except cart_service.CartServiceError as exc:
        _raise_service_error(exc)
