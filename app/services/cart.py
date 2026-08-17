from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.schemas.cart import (
    Cart,
    CartItem,
    CartItemUpsert,
    CompatibilityAssessment,
    CompatibilityReason,
    OrderCard,
    OrderCardItem,
)
from app.schemas.catalog import Money
from app.utils import localized, normalize_locale


class CartServiceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def money(amount: int, currency: str) -> Money:
    formatted = f"₩{amount:,}" if currency == "KRW" else f"{amount:,} {currency}"
    return Money(amount=amount, currency=currency, formatted=formatted)


def _item_options():
    return (
        selectinload(models.MenuItem.translations),
        selectinload(models.MenuItem.ingredient_links).selectinload(
            models.MenuItemIngredient.ingredient
        ),
        selectinload(models.MenuItem.allergen_links).selectinload(models.MenuItemAllergen.allergen),
        selectinload(models.MenuItem.dietary_claims),
    )


def _current_cart_query(user_id: str, restaurant_id: str):
    statement = (
        select(models.Cart)
        .options(selectinload(models.Cart.items))
        .where(
            models.Cart.user_id == user_id,
            models.Cart.restaurant_id == restaurant_id,
            models.Cart.status == "active",
        )
        .order_by(models.Cart.updated_at.desc())
    )
    return statement


def get_current_cart(
    db: Session,
    *,
    user_id: str,
    restaurant_id: str,
) -> models.Cart:
    cart = db.scalars(_current_cart_query(user_id, restaurant_id)).first()
    if cart is None:
        raise CartServiceError("cart_not_found", "There is no active cart.", status_code=404)
    return cart


def get_or_create_cart(
    db: Session,
    *,
    user_id: str,
    restaurant: models.Restaurant,
    table_label: str | None,
    menu_revision: int,
) -> models.Cart:
    cart = db.scalars(_current_cart_query(user_id, restaurant.id)).first()
    if cart is not None and (
        cart.table_label != table_label or cart.menu_revision != menu_revision
    ):
        cart.status = "abandoned"
        cart = None
    if cart is None:
        cart = models.Cart(
            user_id=user_id,
            restaurant_id=restaurant.id,
            serving_mode="dine_in",
            table_label=table_label,
            menu_revision=menu_revision,
            version=1,
            status="active",
        )
        db.add(cart)
        db.flush()
    return cart


def _validate_versions(
    cart: models.Cart,
    restaurant: models.Restaurant,
    *,
    expected_version: int | None,
    expected_menu_revision: int | None,
) -> None:
    if expected_version is not None and expected_version != cart.version:
        raise CartServiceError(
            "cart_version_conflict",
            "The cart changed on another client. Reload it before trying again.",
            status_code=409,
            details={"expected_version": expected_version, "current_version": cart.version},
        )
    requested_revision = expected_menu_revision or cart.menu_revision
    if requested_revision != cart.menu_revision or cart.menu_revision != restaurant.menu_revision:
        raise CartServiceError(
            "menu_revision_conflict",
            "The restaurant menu changed. Reload the menu and review the cart.",
            status_code=409,
            details={
                "expected_menu_revision": requested_revision,
                "cart_menu_revision": cart.menu_revision,
                "current_menu_revision": restaurant.menu_revision,
            },
        )


def _load_menu_items(db: Session, cart: models.Cart) -> dict[str, models.MenuItem]:
    ids = [row.menu_item_id for row in cart.items]
    if not ids:
        return {}
    statement = select(models.MenuItem).options(*_item_options()).where(models.MenuItem.id.in_(ids))
    return {item.id: item for item in db.scalars(statement).unique().all()}


def _translated_item(item: models.MenuItem, locale: str) -> tuple[str, str | None]:
    normalized = normalize_locale(locale)
    translation = next((row for row in item.translations if row.locale == normalized), None)
    if translation:
        return translation.name, item.name_ko
    return localized(item.name_en, item.name_ko, normalized), item.name_ko


def serialize_cart(db: Session, cart: models.Cart, *, locale: str) -> Cart:
    restaurant = db.get(models.Restaurant, cart.restaurant_id)
    if restaurant is None:
        raise CartServiceError(
            "restaurant_not_found", "The restaurant no longer exists.", status_code=404
        )
    menu_items = _load_menu_items(db, cart)
    currency = restaurant.currency
    result_items: list[CartItem] = []
    subtotal = 0
    has_unknown_price = False
    item_count = 0
    for row in cart.items:
        item = menu_items.get(row.menu_item_id)
        if item is None:
            continue
        name, original_name = _translated_item(item, locale)
        unit_price = row.unit_price_snapshot
        line_total = unit_price * row.quantity if unit_price is not None else None
        if line_total is None:
            has_unknown_price = True
        else:
            subtotal += line_total
        item_count += row.quantity
        result_items.append(
            CartItem(
                id=row.id,
                menu_item_id=row.menu_item_id,
                name=name,
                original_name=original_name,
                quantity=row.quantity,
                unit_price=money(unit_price, currency) if unit_price is not None else None,
                line_total=money(line_total, currency) if line_total is not None else None,
                notes=row.notes,
                request_codes=list(row.request_codes or []),
                is_available=item.is_available,
            )
        )
    return Cart(
        id=cart.id,
        restaurant_id=cart.restaurant_id,
        restaurant_name=localized(restaurant.name_en, restaurant.name_ko, locale),
        status=cart.status,
        serving_mode=cart.serving_mode,
        table_label=cart.table_label,
        menu_revision=cart.menu_revision,
        version=cart.version,
        items=result_items,
        item_count=item_count,
        # A partial subtotal would look authoritative while omitting unknown
        # menu prices, so the aggregate is unknown whenever any row is unknown.
        subtotal=None if has_unknown_price else money(subtotal, currency),
        updated_at=cart.updated_at,
    )


def upsert_item(
    db: Session,
    cart: models.Cart,
    menu_item_id: str,
    payload: CartItemUpsert,
) -> models.Cart:
    restaurant = db.get(models.Restaurant, cart.restaurant_id)
    if restaurant is None:
        raise CartServiceError(
            "restaurant_not_found", "The restaurant no longer exists.", status_code=404
        )
    _validate_versions(
        cart,
        restaurant,
        expected_version=payload.expected_version,
        expected_menu_revision=payload.expected_menu_revision,
    )
    item = db.get(models.MenuItem, menu_item_id)
    if item is None or item.restaurant_id != cart.restaurant_id:
        raise CartServiceError(
            "menu_item_not_found", "The menu item was not found.", status_code=404
        )
    if not item.is_available:
        raise CartServiceError(
            "menu_item_unavailable",
            "This menu item is currently unavailable.",
            status_code=409,
            details={"menu_item_id": item.id},
        )
    unsupported_requests = sorted(set(payload.request_codes) - REQUEST_TRANSLATIONS.keys())
    if unsupported_requests:
        raise CartServiceError(
            "unsupported_request_code",
            "One or more structured kitchen requests are unsupported.",
            status_code=422,
            details={"request_codes": unsupported_requests},
        )

    row = next((entry for entry in cart.items if entry.menu_item_id == menu_item_id), None)
    if row is not None and row.unit_price_snapshot != item.price_amount:
        raise CartServiceError(
            "menu_item_price_changed",
            "The menu item price changed. Reload the menu before updating the cart.",
            status_code=409,
            details={
                "menu_item_id": item.id,
                "cart_price": row.unit_price_snapshot,
                "current_price": item.price_amount,
            },
        )
    if row is None:
        row = models.CartItem(
            cart_id=cart.id,
            menu_item_id=item.id,
            quantity=payload.quantity,
            unit_price_snapshot=item.price_amount,
            notes=payload.notes,
            request_codes=payload.request_codes,
        )
        db.add(row)
        cart.items.append(row)
    else:
        row.quantity = payload.quantity
        row.notes = payload.notes
        row.request_codes = payload.request_codes
    cart.version += 1
    db.commit()
    return get_current_cart(db, user_id=cart.user_id, restaurant_id=cart.restaurant_id)


def delete_item(
    db: Session,
    cart: models.Cart,
    menu_item_id: str,
    *,
    expected_version: int | None,
    expected_menu_revision: int | None,
) -> models.Cart:
    restaurant = db.get(models.Restaurant, cart.restaurant_id)
    if restaurant is None:
        raise CartServiceError(
            "restaurant_not_found", "The restaurant no longer exists.", status_code=404
        )
    _validate_versions(
        cart,
        restaurant,
        expected_version=expected_version,
        expected_menu_revision=expected_menu_revision,
    )
    row = next((entry for entry in cart.items if entry.menu_item_id == menu_item_id), None)
    if row is None:
        raise CartServiceError(
            "cart_item_not_found", "The cart item was not found.", status_code=404
        )
    cart.items.remove(row)
    db.delete(row)
    cart.version += 1
    db.commit()
    return get_current_cart(db, user_id=cart.user_id, restaurant_id=cart.restaurant_id)


def update_settings(
    db: Session,
    cart: models.Cart,
    *,
    serving_mode: str,
    expected_version: int | None,
    expected_menu_revision: int | None,
) -> models.Cart:
    restaurant = db.get(models.Restaurant, cart.restaurant_id)
    if restaurant is None:
        raise CartServiceError(
            "restaurant_not_found", "The restaurant no longer exists.", status_code=404
        )
    _validate_versions(
        cart,
        restaurant,
        expected_version=expected_version,
        expected_menu_revision=expected_menu_revision,
    )
    cart.serving_mode = serving_mode
    cart.version += 1
    db.commit()
    return get_current_cart(db, user_id=cart.user_id, restaurant_id=cart.restaurant_id)


def _compatibility(
    passport: models.FoodPassport | None,
    menu_items: dict[str, models.MenuItem],
) -> CompatibilityAssessment:
    disclaimer = (
        "Ingredient and allergen information may be incomplete or subject to cross-contact. "
        "Confirm serious allergies directly with restaurant staff before eating."
    )
    if passport is None:
        return CompatibilityAssessment(status="unknown", reasons=[], disclaimer=disclaimer)

    def normalize_code(value: str) -> str:
        return value.strip().casefold().replace("_", "-").replace(" ", "-")

    allergens = {normalize_code(code) for code in passport.avoid_allergen_codes or []}
    ingredients = {normalize_code(code) for code in passport.avoid_ingredient_codes or []}
    diets = {normalize_code(code) for code in passport.diet_codes or []}
    if not (allergens or ingredients or diets):
        return CompatibilityAssessment(status="compatible", reasons=[], disclaimer=disclaimer)

    reasons: list[CompatibilityReason] = []
    has_conflict = False
    has_unknown = False
    for item in menu_items.values():
        ingredient_codes = {normalize_code(link.ingredient_code) for link in item.ingredient_links}
        expanded_claims: set[str] = set()
        for claim in item.dietary_claims:
            if claim.verification_status in {"unknown", "unverified"}:
                continue
            claim_code = normalize_code(claim.code)
            expanded_claims.update(
                {
                    "vegan": {"vegan", "vegetarian", "pescatarian"},
                    "vegetarian": {"vegetarian", "pescatarian"},
                    "pescatarian": {"pescatarian"},
                }.get(claim_code, {claim_code})
            )
        for code in sorted(ingredients):
            ingredient_aliases = {
                "pork": {"pork", "pork-belly"},
                "dairy": {"dairy", "milk", "cheese"},
            }.get(code, {code})
            if ingredient_aliases.intersection(ingredient_codes):
                has_conflict = True
                reasons.append(
                    CompatibilityReason(
                        code=code,
                        kind="ingredient",
                        message=f"{item.name_en} contains an ingredient you avoid: {code}.",
                        menu_item_id=item.id,
                        relationship="contains",
                    )
                )
            else:
                covering_diets = {
                    "pork": {"vegan", "vegetarian", "pescatarian", "halal"},
                    "pork-belly": {"vegan", "vegetarian", "pescatarian", "halal"},
                    "beef": {"vegan", "vegetarian", "pescatarian"},
                    "chicken": {"vegan", "vegetarian", "pescatarian"},
                    "fish": {"vegan", "vegetarian"},
                    "shellfish": {"vegan", "vegetarian"},
                }.get(code, set())
                if covering_diets.intersection(expanded_claims):
                    continue
                has_unknown = True
                reasons.append(
                    CompatibilityReason(
                        code=code,
                        kind="data_quality",
                        message=(
                            f"Complete ingredient exclusion for {item.name_en} is not verified."
                        ),
                        menu_item_id=item.id,
                    )
                )

        links_by_code: dict[str, list[models.MenuItemAllergen]] = {}
        for link in item.allergen_links:
            links_by_code.setdefault(normalize_code(link.allergen_code), []).append(link)
        for code in sorted(allergens):
            links = links_by_code.get(code, [])
            if not links:
                has_unknown = True
                reasons.append(
                    CompatibilityReason(
                        code=code,
                        kind="data_quality",
                        message=f"Allergen exclusion for {item.name_en} is not verified.",
                        menu_item_id=item.id,
                    )
                )
                continue
            positive_found = False
            negative_verified = False
            for link in links:
                relationship = link.relation_type.casefold()
                if relationship in {"contains", "may_contain", "cross_contact"}:
                    positive_found = True
                    has_conflict = True
                    reasons.append(
                        CompatibilityReason(
                            code=code,
                            kind="allergen",
                            message=f"{item.name_en}: {relationship.replace('_', ' ')} {code}.",
                            menu_item_id=item.id,
                            relationship=relationship,
                        )
                    )
                elif relationship in {"free_from", "does_not_contain"}:
                    negative_verified = link.verification_status not in {"unknown", "unverified"}
            if not positive_found and not negative_verified:
                has_unknown = True
                reasons.append(
                    CompatibilityReason(
                        code=code,
                        kind="data_quality",
                        message=f"Allergen exclusion for {item.name_en} is not verified.",
                        menu_item_id=item.id,
                    )
                )

        for code in sorted(diets - expanded_claims):
            has_unknown = True
            reasons.append(
                CompatibilityReason(
                    code=code,
                    kind="diet",
                    message=f"{item.name_en} has no verified {code} claim.",
                    menu_item_id=item.id,
                )
            )

    status = "conflict" if has_conflict else "unknown" if has_unknown else "compatible"
    return CompatibilityAssessment(status=status, reasons=reasons, disclaimer=disclaimer)


REQUEST_TRANSLATIONS: dict[str, tuple[str, str]] = {
    "less_spicy": ("덜 맵게 해 주세요", "Please make it less spicy"),
    "no_pork": ("돼지고기는 빼 주세요", "Please leave out pork"),
    "no_peanuts": ("땅콩은 빼 주세요", "Please leave out peanuts"),
    "no_dairy": ("유제품은 빼 주세요", "Please leave out dairy"),
}


def build_order_card(
    db: Session,
    cart: models.Cart,
    *,
    locale: str,
    expected_version: int | None = None,
    expected_menu_revision: int | None = None,
    order_id: str | None = None,
) -> OrderCard:
    restaurant = db.get(models.Restaurant, cart.restaurant_id)
    if restaurant is None:
        raise CartServiceError(
            "restaurant_not_found", "The restaurant no longer exists.", status_code=404
        )
    _validate_versions(
        cart,
        restaurant,
        expected_version=expected_version,
        expected_menu_revision=expected_menu_revision,
    )
    if not cart.items:
        raise CartServiceError("empty_cart", "Add at least one item first.", status_code=422)
    menu_items = _load_menu_items(db, cart)
    conflicts: list[dict[str, object]] = []
    for row in cart.items:
        item = menu_items.get(row.menu_item_id)
        if item is None:
            conflicts.append({"menu_item_id": row.menu_item_id, "reason": "removed"})
        elif not item.is_available:
            conflicts.append({"menu_item_id": row.menu_item_id, "reason": "unavailable"})
        elif item.price_amount != row.unit_price_snapshot:
            conflicts.append(
                {
                    "menu_item_id": row.menu_item_id,
                    "reason": "price_changed",
                    "cart_price": row.unit_price_snapshot,
                    "current_price": item.price_amount,
                }
            )
    if conflicts:
        raise CartServiceError(
            "cart_requires_review",
            "Availability or prices changed. Review the cart before preparing the order card.",
            status_code=409,
            details={"items": conflicts},
        )

    currency = restaurant.currency
    output_items: list[OrderCardItem] = []
    korean_parts: list[str] = []
    translated_parts: list[str] = []
    subtotal = 0
    has_unknown_price = False
    request_codes: list[str] = []
    for row in cart.items:
        item = menu_items[row.menu_item_id]
        display_name, _ = _translated_item(item, locale)
        korean_name = item.name_ko or item.name_en
        unit_price = item.price_amount
        line_total = unit_price * row.quantity if unit_price is not None else None
        if line_total is None:
            has_unknown_price = True
        else:
            subtotal += line_total
        korean_parts.append(f"{korean_name} {row.quantity}개")
        translated_parts.append(f"{display_name} × {row.quantity}")
        for code in row.request_codes or []:
            if code not in request_codes:
                request_codes.append(code)
        output_items.append(
            OrderCardItem(
                menu_item_id=item.id,
                name=display_name,
                original_name=korean_name,
                quantity=row.quantity,
                unit_price=money(unit_price, currency) if unit_price is not None else None,
                line_total=money(line_total, currency) if line_total is not None else None,
                notes=row.notes,
                request_codes=list(row.request_codes or []),
            )
        )

    known_requests = [
        REQUEST_TRANSLATIONS[code] for code in request_codes if code in REQUEST_TRANSLATIONS
    ]
    request_ko = ". ".join(value[0] for value in known_requests) or None
    request_localized = ". ".join(value[1] for value in known_requests) or None
    passport = db.get(models.FoodPassport, cart.user_id)
    assessment = _compatibility(passport, menu_items)
    return OrderCard(
        order_id=order_id,
        status="prepared" if order_id else "preview",
        cart_id=cart.id,
        cart_version=cart.version,
        menu_revision=cart.menu_revision,
        restaurant_id=restaurant.id,
        restaurant_name=localized(restaurant.name_en, restaurant.name_ko, locale),
        table_label=cart.table_label,
        serving_mode=cart.serving_mode,
        items=output_items,
        subtotal=None if has_unknown_price else money(subtotal, currency),
        total=None if has_unknown_price else money(subtotal, currency),
        korean_phrase=", ".join(korean_parts) + " 주세요",
        translated_phrase=", ".join(translated_parts) + ", please",
        request_note_ko=request_ko,
        request_note_localized=request_localized,
        compatibility=assessment,
        generated_at=datetime.now(timezone.utc),
    )


def prepare_order_card(
    db: Session,
    cart: models.Cart,
    *,
    locale: str,
    idempotency_key: str,
    expected_version: int | None,
    expected_menu_revision: int | None,
) -> OrderCard:
    normalized_locale = normalize_locale(locale)
    cart_id = cart.id
    user_id = cart.user_id
    restaurant_id = cart.restaurant_id
    serving_mode = cart.serving_mode
    fingerprint = _order_card_request_fingerprint(
        cart_id=cart_id,
        expected_version=expected_version,
        expected_menu_revision=expected_menu_revision,
        locale=normalized_locale,
    )
    existing = _idempotent_order(db, user_id=user_id, idempotency_key=idempotency_key)
    if existing is not None:
        return _replay_order_card(existing, fingerprint)

    preview = build_order_card(
        db,
        cart,
        locale=normalized_locale,
        expected_version=expected_version,
        expected_menu_revision=expected_menu_revision,
    )
    order_id = models.new_id()
    prepared = preview.model_copy(update={"order_id": order_id, "status": "prepared"})
    response_snapshot = prepared.model_dump(mode="json")
    currency = (
        preview.total.currency
        if preview.total is not None
        else next(
            (
                item.unit_price.currency
                for item in preview.items
                if item.unit_price is not None
            ),
            None,
        )
    )
    if currency is None:
        restaurant = db.get(models.Restaurant, restaurant_id)
        if restaurant is None:
            raise CartServiceError(
                "restaurant_not_found", "The restaurant no longer exists.", status_code=404
            )
        currency = restaurant.currency
    order = models.Order(
        id=order_id,
        user_id=user_id,
        restaurant_id=restaurant_id,
        status="prepared",
        serving_mode=serving_mode,
        subtotal_amount=preview.subtotal.amount if preview.subtotal is not None else None,
        total_amount=preview.total.amount if preview.total is not None else None,
        currency=currency,
        korean_phrase=preview.korean_phrase,
        translated_phrase=preview.translated_phrase,
        allergy_note_ko=preview.request_note_ko,
        allergy_note_localized=preview.request_note_localized,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        response_snapshot=response_snapshot,
    )
    db.add(order)
    for row in preview.items:
        db.add(
            models.OrderItem(
                order_id=order.id,
                menu_item_id=row.menu_item_id,
                name_en_snapshot=row.name,
                name_ko_snapshot=row.original_name,
                unit_price_amount=(
                    row.unit_price.amount if row.unit_price is not None else None
                ),
                quantity=row.quantity,
                notes=row.notes,
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_idempotency_unique_violation(exc):
            raise
        raced = _idempotent_order(
            db,
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
        if raced is None:
            raise
        return _replay_order_card(raced, fingerprint)
    return prepared


def _order_card_request_fingerprint(
    *,
    cart_id: str,
    expected_version: int | None,
    expected_menu_revision: int | None,
    locale: str,
) -> str:
    canonical = json.dumps(
        {
            "cart_id": cart_id,
            "expected_menu_revision": expected_menu_revision,
            "expected_version": expected_version,
            "locale": normalize_locale(locale),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotent_order(
    db: Session,
    *,
    user_id: str,
    idempotency_key: str,
) -> models.Order | None:
    return db.scalar(
        select(models.Order)
        .where(
            models.Order.user_id == user_id,
            models.Order.idempotency_key == idempotency_key,
        )
    )


def _replay_order_card(order: models.Order, fingerprint: str) -> OrderCard:
    if order.request_fingerprint != fingerprint:
        raise CartServiceError(
            "idempotency_key_conflict",
            "This idempotency key was already used for a different order-card request.",
            status_code=409,
        )
    return OrderCard.model_validate(order.response_snapshot)


def _is_idempotency_unique_violation(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name in {
        "orders_user_id_idempotency_key_key",
        "uq_orders_user_id_idempotency_key",
    }:
        return True
    normalized = "".join(str(exc.orig).casefold().split()).replace('"', "").replace("`", "")
    return "uniqueconstraintfailed:orders.user_id,orders.idempotency_key" in normalized
