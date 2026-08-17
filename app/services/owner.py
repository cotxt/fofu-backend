from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import models
from app.schemas.owner import (
    MediaAssetResponse,
    MenuAvailabilityPatch,
    OpeningHourResponse,
    OpeningHoursPatch,
    OpenStatusPatch,
    OwnerApplicationCreate,
    OwnerApplicationListResponse,
    OwnerApplicationResponse,
    OwnerDashboardResponse,
    OwnerMenuItemResponse,
    OwnerRestaurantListResponse,
    OwnerRestaurantSummary,
)

_ACTIVE_APPLICATION_STATUSES = {"pending", "submitted", "under_review"}
_READ_ROLES = {"owner", "manager", "staff"}
_WRITE_ROLES = {"owner", "manager"}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


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


def create_media_asset_record(
    db: Session,
    user: models.User,
    *,
    purpose: str,
    storage_key: str,
    original_filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
) -> MediaAssetResponse:
    asset = models.MediaAsset(
        owner_user_id=user.id,
        purpose=purpose,
        storage_key=storage_key,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        status="uploaded",
    )
    db.add(asset)
    db.flush()
    _audit(
        db,
        actor_user_id=user.id,
        action="media.uploaded",
        resource_type="media_asset",
        resource_id=asset.id,
        details={
            "purpose": purpose,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
    )
    db.commit()
    db.refresh(asset)
    return MediaAssetResponse.model_validate(asset)


def create_owner_application(
    db: Session,
    user: models.User,
    payload: OwnerApplicationCreate,
) -> OwnerApplicationResponse:
    if user.is_guest:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "account_required",
            "Create a full account before applying for merchant access.",
        )

    media = db.get(models.MediaAsset, payload.license_media_id)
    if (
        media is None
        or media.owner_user_id != user.id
        or media.purpose != "business_license"
        or media.status != "uploaded"
        or media.content_type not in {"image/jpeg", "image/png", "application/pdf"}
    ):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_license_media",
            "Upload a valid business-license file owned by this account.",
        )

    restaurant = db.get(models.Restaurant, payload.restaurant_id)
    if restaurant is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "restaurant_not_found",
            "Restaurant not found.",
        )
    if restaurant.owner_user_id is not None:
        membership = db.get(
            models.RestaurantMembership,
            {"restaurant_id": restaurant.id, "user_id": user.id},
        )
        if (
            membership is None
            or membership.status != "active"
            or membership.role not in _WRITE_ROLES
        ):
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "restaurant_claim_forbidden",
                "You cannot submit an application for this restaurant.",
            )

    duplicate = db.scalar(
        select(models.OwnerApplication.id).where(
            models.OwnerApplication.registration_number == payload.registration_number,
            models.OwnerApplication.status.in_(_ACTIVE_APPLICATION_STATUSES),
        )
    )
    if duplicate is not None:
        raise _error(
            status.HTTP_409_CONFLICT,
            "application_already_pending",
            "An application for this business is already under review.",
        )

    application = models.OwnerApplication(
        applicant_user_id=user.id,
        restaurant_id=payload.restaurant_id,
        business_name=payload.business_name,
        registration_number=payload.registration_number,
        address=payload.address,
        phone=payload.phone,
        license_media_id=payload.license_media_id,
        agreed_to_terms_at=models.utcnow(),
        terms_version=payload.terms_version,
        phone_verified_at=None,
        # Approval is exclusively an administrative review action.
        status="pending",
    )
    db.add(application)
    db.flush()
    _audit(
        db,
        actor_user_id=user.id,
        action="owner_application.created",
        resource_type="owner_application",
        resource_id=application.id,
        details={
            "restaurant_id": application.restaurant_id,
            "license_media_id": application.license_media_id,
            "terms_version": application.terms_version,
            "status": "pending",
        },
    )
    db.commit()
    db.refresh(application)
    return OwnerApplicationResponse.model_validate(application)


def list_owner_applications(
    db: Session, user: models.User
) -> OwnerApplicationListResponse:
    applications = db.scalars(
        select(models.OwnerApplication)
        .where(models.OwnerApplication.applicant_user_id == user.id)
        .order_by(models.OwnerApplication.created_at.desc(), models.OwnerApplication.id.desc())
    ).all()
    return OwnerApplicationListResponse(
        items=[OwnerApplicationResponse.model_validate(item) for item in applications]
    )


def get_owner_application(
    db: Session, user: models.User, application_id: str
) -> OwnerApplicationResponse:
    application = db.scalar(
        select(models.OwnerApplication).where(
            models.OwnerApplication.id == application_id,
            models.OwnerApplication.applicant_user_id == user.id,
        )
    )
    if application is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "owner_application_not_found",
            "Owner application not found.",
        )
    return OwnerApplicationResponse.model_validate(application)


def _require_restaurant_access(
    db: Session,
    user: models.User,
    restaurant_id: str,
    *,
    write: bool,
) -> tuple[models.Restaurant, str]:
    restaurant = db.get(models.Restaurant, restaurant_id)
    if restaurant is None:
        raise _error(status.HTTP_404_NOT_FOUND, "restaurant_not_found", "Restaurant not found.")

    if "admin" in (user.roles or []):
        return restaurant, "admin"

    membership = db.get(
        models.RestaurantMembership,
        {"restaurant_id": restaurant_id, "user_id": user.id},
    )
    allowed_roles = _WRITE_ROLES if write else _READ_ROLES
    if (
        membership is None
        or membership.status != "active"
        or membership.role not in allowed_roles
        or (membership.role == "owner" and restaurant.owner_user_id != user.id)
    ):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "restaurant_access_forbidden",
            "An active restaurant membership with the required role is needed.",
        )
    return restaurant, membership.role


def _restaurant_summary(restaurant: models.Restaurant) -> OwnerRestaurantSummary:
    return OwnerRestaurantSummary(
        id=restaurant.id,
        slug=restaurant.slug,
        name_en=restaurant.name_en,
        name_ko=restaurant.name_ko,
        category=restaurant.category,
        address_en=restaurant.address_en,
        address_ko=restaurant.address_ko,
        cover_image_url=restaurant.cover_image_url,
        rating_avg=float(restaurant.rating_avg),
        rating_count=restaurant.rating_count,
        is_verified=restaurant.is_verified,
        is_open=restaurant.is_open,
        is_published=restaurant.is_published,
        menu_revision=restaurant.menu_revision,
    )


def list_managed_restaurants(
    db: Session, user: models.User
) -> OwnerRestaurantListResponse:
    """Return restaurants exposed by the user's current active memberships.

    The global ``owner`` role is deliberately insufficient: membership is the
    restaurant-scoped source of truth. Canonical owner rows must also agree
    with ``restaurants.owner_user_id`` so stale legacy memberships cannot
    disclose a restaurant dashboard.
    """

    rows = db.execute(
        select(models.Restaurant, models.RestaurantMembership.role)
        .join(
            models.RestaurantMembership,
            models.RestaurantMembership.restaurant_id == models.Restaurant.id,
        )
        .where(
            models.RestaurantMembership.user_id == user.id,
            models.RestaurantMembership.status == "active",
            models.RestaurantMembership.role.in_(_READ_ROLES),
        )
        .order_by(models.Restaurant.name_en, models.Restaurant.id)
    ).all()
    restaurants = [
        restaurant
        for restaurant, role in rows
        if role != "owner" or restaurant.owner_user_id == user.id
    ]
    return OwnerRestaurantListResponse(
        items=[_restaurant_summary(restaurant) for restaurant in restaurants]
    )


def list_application_restaurants(
    db: Session,
    *,
    query: str | None,
) -> OwnerRestaurantListResponse:
    """List currently unclaimed restaurants eligible for an owner application.

    Draft restaurants are intentionally included. The catalog importer creates new
    venues unpublished so an applicant can claim the exact pre-reviewed row before
    an administrator verifies and publishes it.
    """

    active_owner_exists = (
        select(models.RestaurantMembership.restaurant_id)
        .where(
            models.RestaurantMembership.restaurant_id == models.Restaurant.id,
            models.RestaurantMembership.role == "owner",
            models.RestaurantMembership.status == "active",
        )
        .exists()
    )
    filters: list[object] = [
        models.Restaurant.owner_user_id.is_(None),
        ~active_owner_exists,
    ]
    if query is not None and (normalized := " ".join(query.split())):
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        filters.append(
            or_(
                models.Restaurant.name_en.ilike(pattern, escape="\\"),
                models.Restaurant.name_ko.ilike(pattern, escape="\\"),
                models.Restaurant.address_en.ilike(pattern, escape="\\"),
                models.Restaurant.address_ko.ilike(pattern, escape="\\"),
                models.Restaurant.slug.ilike(pattern, escape="\\"),
            )
        )
    restaurants = list(
        db.scalars(
            select(models.Restaurant)
            .where(*filters)
            .order_by(models.Restaurant.name_en, models.Restaurant.id)
            .limit(50)
        ).all()
    )
    return OwnerRestaurantListResponse(
        items=[_restaurant_summary(restaurant) for restaurant in restaurants]
    )


def get_dashboard(
    db: Session, user: models.User, restaurant_id: str
) -> OwnerDashboardResponse:
    restaurant, role = _require_restaurant_access(
        db, user, restaurant_id, write=False
    )
    hours = list(
        db.scalars(
            select(models.OpeningHour)
            .where(models.OpeningHour.restaurant_id == restaurant.id)
            .order_by(models.OpeningHour.day_of_week)
        ).all()
    )
    menu_items = list(
        db.scalars(
            select(models.MenuItem)
            .where(models.MenuItem.restaurant_id == restaurant.id)
            .order_by(models.MenuItem.sort_order, models.MenuItem.id)
        ).all()
    )
    published_review_count = int(
        db.scalar(
            select(func.count(models.Review.id))
            .join(models.MenuItem, models.MenuItem.id == models.Review.menu_item_id)
            .where(
                models.MenuItem.restaurant_id == restaurant.id,
                models.Review.is_published.is_(True),
            )
        )
        or 0
    )
    new_since = models.utcnow() - timedelta(days=7)
    new_review_count = int(
        db.scalar(
            select(func.count(models.Review.id))
            .join(models.MenuItem, models.MenuItem.id == models.Review.menu_item_id)
            .where(
                models.MenuItem.restaurant_id == restaurant.id,
                models.Review.is_published.is_(True),
                models.Review.created_at >= new_since,
            )
        )
        or 0
    )
    weekly_views = int(
        db.scalar(
            select(func.count(models.QRScan.id))
            .join(models.QRCode, models.QRCode.id == models.QRScan.qr_code_id)
            .where(
                models.QRCode.restaurant_id == restaurant.id,
                models.QRScan.scanned_at >= new_since,
            )
        )
        or 0
    )
    return OwnerDashboardResponse(
        restaurant=_restaurant_summary(restaurant),
        membership_role=role,
        hours=[OpeningHourResponse.model_validate(item) for item in hours],
        menu_items=[
            OwnerMenuItemResponse(
                id=item.id,
                category_id=item.category_id,
                slug=item.slug,
                name_en=item.name_en,
                name_ko=item.name_ko,
                price_amount=item.price_amount,
                currency=item.currency,
                image_url=item.image_url,
                is_available=item.is_available,
                sort_order=item.sort_order,
            )
            for item in menu_items
        ],
        menu_item_count=len(menu_items),
        photo_count=len(restaurant.gallery or []),
        published_review_count=published_review_count,
        new_review_count=new_review_count,
        weekly_views=weekly_views,
    )


def update_open_status(
    db: Session,
    user: models.User,
    restaurant_id: str,
    payload: OpenStatusPatch,
) -> OwnerRestaurantSummary:
    restaurant, role = _require_restaurant_access(db, user, restaurant_id, write=True)
    previous = restaurant.is_open
    restaurant.is_open = payload.is_open
    _audit(
        db,
        actor_user_id=user.id,
        action="restaurant.open_status_updated",
        resource_type="restaurant",
        resource_id=restaurant.id,
        details={"from": previous, "to": payload.is_open, "membership_role": role},
    )
    db.commit()
    db.refresh(restaurant)
    return _restaurant_summary(restaurant)


def update_opening_hours(
    db: Session,
    user: models.User,
    restaurant_id: str,
    payload: OpeningHoursPatch,
) -> list[OpeningHourResponse]:
    restaurant, role = _require_restaurant_access(db, user, restaurant_id, write=True)
    existing = {
        hour.day_of_week: hour
        for hour in db.scalars(
            select(models.OpeningHour).where(
                models.OpeningHour.restaurant_id == restaurant.id
            )
        ).all()
    }
    for update in payload.hours:
        hour = existing.get(update.day_of_week)
        if hour is None:
            hour = models.OpeningHour(
                restaurant_id=restaurant.id,
                day_of_week=update.day_of_week,
            )
            db.add(hour)
            existing[update.day_of_week] = hour
        hour.opens_at = update.opens_at
        hour.closes_at = update.closes_at
        hour.is_closed = update.is_closed

    _audit(
        db,
        actor_user_id=user.id,
        action="restaurant.hours_updated",
        resource_type="restaurant",
        resource_id=restaurant.id,
        details={
            "days": sorted(update.day_of_week for update in payload.hours),
            "membership_role": role,
        },
    )
    db.commit()
    refreshed = db.scalars(
        select(models.OpeningHour)
        .where(models.OpeningHour.restaurant_id == restaurant.id)
        .order_by(models.OpeningHour.day_of_week)
    ).all()
    return [OpeningHourResponse.model_validate(item) for item in refreshed]


def update_menu_availability(
    db: Session,
    user: models.User,
    restaurant_id: str,
    menu_item_id: str,
    payload: MenuAvailabilityPatch,
) -> OwnerMenuItemResponse:
    restaurant, role = _require_restaurant_access(db, user, restaurant_id, write=True)
    item = db.scalar(
        select(models.MenuItem).where(
            models.MenuItem.id == menu_item_id,
            models.MenuItem.restaurant_id == restaurant.id,
        )
    )
    if item is None:
        raise _error(status.HTTP_404_NOT_FOUND, "menu_item_not_found", "Menu item not found.")
    previous = item.is_available
    previous_revision = restaurant.menu_revision
    item.is_available = payload.is_available
    if previous != payload.is_available:
        restaurant.menu_revision = previous_revision + 1
    _audit(
        db,
        actor_user_id=user.id,
        action="menu_item.availability_updated",
        resource_type="menu_item",
        resource_id=item.id,
        details={
            "restaurant_id": restaurant.id,
            "from": previous,
            "to": payload.is_available,
            "menu_revision_from": previous_revision,
            "menu_revision_to": restaurant.menu_revision,
            "membership_role": role,
        },
    )
    db.commit()
    db.refresh(item)
    return OwnerMenuItemResponse(
        id=item.id,
        category_id=item.category_id,
        slug=item.slug,
        name_en=item.name_en,
        name_ko=item.name_ko,
        price_amount=item.price_amount,
        currency=item.currency,
        image_url=item.image_url,
        is_available=item.is_available,
        sort_order=item.sort_order,
    )
