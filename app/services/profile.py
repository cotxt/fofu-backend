from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.schemas.profile import (
    OrderHistoryItemResponse,
    OrderHistoryListResponse,
    OrderHistoryResponse,
    PassportPatch,
    PassportResponse,
    ProfilePatch,
    ProfileResponse,
    SavedRestaurantListResponse,
    SavedRestaurantResponse,
)
from app.utils import decode_cursor, encode_cursor, localized


def _audit(
    db: Session,
    *,
    actor_user_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        models.AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )


def get_profile(user: models.User) -> ProfileResponse:
    return ProfileResponse.model_validate(user)


def update_profile(db: Session, user: models.User, payload: ProfilePatch) -> ProfileResponse:
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(user, field, value)

    _audit(
        db,
        actor_user_id=user.id,
        action="profile.updated",
        resource_type="user",
        resource_id=user.id,
        details={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(user)
    return ProfileResponse.model_validate(user)


def get_passport(db: Session, user: models.User) -> PassportResponse:
    passport = db.get(models.FoodPassport, user.id)
    if passport is None:
        passport = models.FoodPassport(user_id=user.id)
        db.add(passport)
        _audit(
            db,
            actor_user_id=user.id,
            action="passport.created",
            resource_type="food_passport",
            resource_id=user.id,
        )
        db.commit()
        db.refresh(passport)
    return PassportResponse.model_validate(passport)


def update_passport(
    db: Session, user: models.User, payload: PassportPatch
) -> PassportResponse:
    changes = payload.model_dump(exclude_unset=True)
    expected_version = changes.pop("version", None)

    passport = db.get(models.FoodPassport, user.id)
    created = passport is None
    current_version = passport.version if passport is not None else 0
    if expected_version is not None and expected_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "passport_version_conflict",
                "message": "The food passport changed since it was last read.",
                "details": {"current_version": current_version},
            },
        )
    if passport is None:
        passport = models.FoodPassport(user_id=user.id)
        db.add(passport)
    for field, value in changes.items():
        setattr(passport, field, value)
    passport.version = 1 if created else (passport.version or 1) + 1

    _audit(
        db,
        actor_user_id=user.id,
        action="passport.created" if created else "passport.updated",
        resource_type="food_passport",
        resource_id=user.id,
        details={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.model_validate(passport)


def _saved_response(
    restaurant: models.Restaurant, *, saved_at: datetime, locale: str
) -> SavedRestaurantResponse:
    return SavedRestaurantResponse(
        id=restaurant.id,
        slug=restaurant.slug,
        name=localized(restaurant.name_en, restaurant.name_ko, locale),
        name_en=restaurant.name_en,
        name_ko=restaurant.name_ko,
        category=restaurant.category,
        address=localized(restaurant.address_en, restaurant.address_ko, locale),
        address_en=restaurant.address_en,
        address_ko=restaurant.address_ko,
        latitude=restaurant.latitude,
        longitude=restaurant.longitude,
        rating_avg=float(restaurant.rating_avg),
        rating_count=restaurant.rating_count,
        is_verified=restaurant.is_verified,
        is_open=restaurant.is_open,
        cover_image_url=restaurant.cover_image_url,
        saved_at=saved_at,
    )


def list_saved_restaurants(
    db: Session,
    user: models.User,
    *,
    cursor: str | None,
    limit: int,
) -> SavedRestaurantListResponse:
    offset = decode_cursor(cursor)
    statement: Select[tuple[models.SavedRestaurant, models.Restaurant]] = (
        select(models.SavedRestaurant, models.Restaurant)
        .join(
            models.Restaurant,
            models.Restaurant.id == models.SavedRestaurant.restaurant_id,
        )
        .where(
            models.SavedRestaurant.user_id == user.id,
            models.Restaurant.is_published.is_(True),
        )
        .order_by(
            models.SavedRestaurant.created_at.desc(),
            models.SavedRestaurant.restaurant_id.desc(),
        )
        .offset(offset)
        .limit(limit + 1)
    )
    rows = list(db.execute(statement).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return SavedRestaurantListResponse(
        items=[
            _saved_response(restaurant, saved_at=saved.created_at, locale=user.locale)
            for saved, restaurant in rows
        ],
        next_cursor=encode_cursor(offset + limit) if has_more else None,
        has_more=has_more,
    )


def save_restaurant(
    db: Session, user: models.User, restaurant_id: str
) -> SavedRestaurantResponse:
    restaurant = db.get(models.Restaurant, restaurant_id)
    if restaurant is None or not restaurant.is_published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "restaurant_not_found", "message": "Restaurant not found."},
        )

    key = {"user_id": user.id, "restaurant_id": restaurant.id}
    saved = db.get(models.SavedRestaurant, key)
    if saved is None:
        saved = models.SavedRestaurant(**key)
        db.add(saved)
        _audit(
            db,
            actor_user_id=user.id,
            action="restaurant.saved",
            resource_type="restaurant",
            resource_id=restaurant.id,
        )
        try:
            db.commit()
            db.refresh(saved)
        except IntegrityError:
            # Concurrent PUTs are still idempotent; the winning row is returned.
            db.rollback()
            saved = db.get(models.SavedRestaurant, key)
            if saved is None:
                raise
    return _saved_response(restaurant, saved_at=saved.created_at, locale=user.locale)


def unsave_restaurant(db: Session, user: models.User, restaurant_id: str) -> None:
    key = {"user_id": user.id, "restaurant_id": restaurant_id}
    saved = db.get(models.SavedRestaurant, key)
    if saved is None:
        return
    db.delete(saved)
    _audit(
        db,
        actor_user_id=user.id,
        action="restaurant.unsaved",
        resource_type="restaurant",
        resource_id=restaurant_id,
    )
    db.commit()


def list_order_history(
    db: Session,
    user: models.User,
    *,
    cursor: str | None,
    limit: int,
) -> OrderHistoryListResponse:
    offset = decode_cursor(cursor)
    rows = list(
        db.execute(
            select(models.Order, models.Restaurant)
            .join(models.Restaurant, models.Restaurant.id == models.Order.restaurant_id)
            .options(selectinload(models.Order.items))
            .where(
                models.Order.user_id == user.id,
                models.Order.status == "prepared",
            )
            .order_by(models.Order.created_at.desc(), models.Order.id.desc())
            .offset(offset)
            .limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    orders: list[OrderHistoryResponse] = []
    for order, restaurant in rows:
        order_items = sorted(order.items, key=lambda item: item.id)
        orders.append(
            OrderHistoryResponse(
                id=order.id,
                restaurant_id=restaurant.id,
                restaurant_slug=restaurant.slug,
                restaurant_name=localized(
                    restaurant.name_en,
                    restaurant.name_ko,
                    user.locale,
                ),
                status=order.status,
                serving_mode=order.serving_mode,
                table_label=order.response_snapshot.get("table_label"),
                subtotal_amount=order.subtotal_amount,
                total_amount=order.total_amount,
                currency=order.currency,
                korean_phrase=order.korean_phrase,
                translated_phrase=order.translated_phrase,
                allergy_note_ko=order.allergy_note_ko,
                allergy_note_localized=order.allergy_note_localized,
                items=[
                    OrderHistoryItemResponse(
                        id=item.id,
                        menu_item_id=item.menu_item_id,
                        name=localized(
                            item.name_en_snapshot,
                            item.name_ko_snapshot,
                            user.locale,
                        ),
                        name_en_snapshot=item.name_en_snapshot,
                        name_ko_snapshot=item.name_ko_snapshot,
                        unit_price_amount=item.unit_price_amount,
                        line_total_amount=(
                            item.unit_price_amount * item.quantity
                            if item.unit_price_amount is not None
                            else None
                        ),
                        quantity=item.quantity,
                        notes=item.notes,
                    )
                    for item in order_items
                ],
                item_count=sum(item.quantity for item in order_items),
                created_at=order.created_at,
                updated_at=order.updated_at,
            )
        )
    return OrderHistoryListResponse(
        items=orders,
        next_cursor=encode_cursor(offset + limit) if has_more else None,
        has_more=has_more,
    )
