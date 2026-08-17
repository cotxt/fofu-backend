from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.schemas.admin import (
    AdminAuditEventListResponse,
    AdminAuditEventResponse,
    AdminOverviewResponse,
    AdminOwnerApplicationListResponse,
    AdminOwnerApplicationResponse,
    AdminOwnerApplicationReview,
    AdminRestaurantListResponse,
    AdminRestaurantModerationUpdate,
    AdminRestaurantResponse,
    AdminUserListResponse,
    AdminUserResponse,
)

_APPLICATION_STATUSES = {"pending", "submitted", "under_review", "approved", "rejected"}
_FINAL_APPLICATION_STATUSES = {"approved", "rejected"}


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


def _count(db: Session, column: object, *filters: object) -> int:
    return int(db.scalar(select(func.count(column)).where(*filters)) or 0)


def get_overview(db: Session) -> AdminOverviewResponse:
    return AdminOverviewResponse(
        users_total=_count(db, models.User.id),
        users_active=_count(db, models.User.id, models.User.is_active.is_(True)),
        restaurants_total=_count(db, models.Restaurant.id),
        restaurants_published=_count(
            db, models.Restaurant.id, models.Restaurant.is_published.is_(True)
        ),
        owner_applications_pending=_count(
            db,
            models.OwnerApplication.id,
            models.OwnerApplication.status.in_({"pending", "submitted"}),
        ),
        owner_applications_under_review=_count(
            db,
            models.OwnerApplication.id,
            models.OwnerApplication.status == "under_review",
        ),
        audit_events_total=_count(db, models.AuditEvent.id),
    )


def _search_pattern(query: str | None) -> str | None:
    if query is None:
        return None
    normalized = query.strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_users(
    db: Session, *, query: str | None, limit: int, offset: int
) -> AdminUserListResponse:
    filters: list[object] = []
    if pattern := _search_pattern(query):
        filters.append(
            or_(
                models.User.email.ilike(pattern, escape="\\"),
                models.User.display_name.ilike(pattern, escape="\\"),
            )
        )
    users = list(
        db.scalars(
            select(models.User)
            .where(*filters)
            .order_by(models.User.created_at.desc(), models.User.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    total = int(db.scalar(select(func.count(models.User.id)).where(*filters)) or 0)
    return AdminUserListResponse(
        items=[
            AdminUserResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                locale=user.locale,
                is_guest=user.is_guest,
                is_active=user.is_active,
                roles=list(user.roles or []),
                created_at=user.created_at,
            )
            for user in users
        ],
        total=total,
    )


def list_restaurants(
    db: Session, *, query: str | None, limit: int, offset: int
) -> AdminRestaurantListResponse:
    filters: list[object] = []
    if pattern := _search_pattern(query):
        filters.append(
            or_(
                models.Restaurant.name_en.ilike(pattern, escape="\\"),
                models.Restaurant.name_ko.ilike(pattern, escape="\\"),
                models.Restaurant.slug.ilike(pattern, escape="\\"),
            )
        )
    restaurants = list(
        db.scalars(
            select(models.Restaurant)
            .where(*filters)
            .order_by(models.Restaurant.created_at.desc(), models.Restaurant.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    total = int(db.scalar(select(func.count(models.Restaurant.id)).where(*filters)) or 0)
    return AdminRestaurantListResponse(
        items=[_restaurant_response(restaurant) for restaurant in restaurants],
        total=total,
    )


def _restaurant_response(restaurant: models.Restaurant) -> AdminRestaurantResponse:
    return AdminRestaurantResponse(
        id=restaurant.id,
        slug=restaurant.slug,
        name_en=restaurant.name_en,
        name_ko=restaurant.name_ko,
        owner_user_id=restaurant.owner_user_id,
        is_verified=restaurant.is_verified,
        is_published=restaurant.is_published,
        is_open=restaurant.is_open,
        created_at=restaurant.created_at,
    )


def update_restaurant_moderation(
    db: Session,
    admin: models.User,
    restaurant_id: str,
    payload: AdminRestaurantModerationUpdate,
) -> AdminRestaurantResponse:
    restaurant = db.scalar(
        select(models.Restaurant)
        .where(models.Restaurant.id == restaurant_id)
        .with_for_update()
    )
    if restaurant is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "restaurant_not_found",
            "Restaurant not found.",
        )

    requested = payload.model_dump(exclude_unset=True)
    changes: dict[str, dict[str, bool]] = {}
    for field, value in requested.items():
        # The request schema rejects null, so this branch exists only to keep the
        # assignment type narrow for static analysis.
        if value is None:
            continue
        previous = bool(getattr(restaurant, field))
        setattr(restaurant, field, value)
        changes[field] = {"from": previous, "to": value}

    _audit(
        db,
        actor_user_id=admin.id,
        action="restaurant.moderation_updated",
        resource_type="restaurant",
        resource_id=restaurant.id,
        details={"changes": changes},
    )
    db.commit()
    db.refresh(restaurant)
    return _restaurant_response(restaurant)


def _application_response(
    db: Session, application: models.OwnerApplication
) -> AdminOwnerApplicationResponse:
    applicant = db.get(models.User, application.applicant_user_id)
    license_asset = db.get(models.MediaAsset, application.license_media_id)
    restaurant = (
        db.get(models.Restaurant, application.restaurant_id)
        if application.restaurant_id is not None
        else None
    )
    if applicant is None or license_asset is None:
        raise _error(
            status.HTTP_409_CONFLICT,
            "owner_application_data_missing",
            "The owner application references data that is no longer available.",
        )
    return AdminOwnerApplicationResponse(
        id=application.id,
        applicant_user_id=application.applicant_user_id,
        applicant_email=applicant.email,
        applicant_display_name=applicant.display_name,
        restaurant_id=application.restaurant_id,
        restaurant_name=restaurant.name_en if restaurant is not None else None,
        business_name=application.business_name,
        registration_number=application.registration_number,
        address=application.address,
        phone=application.phone,
        license_media_id=application.license_media_id,
        license_original_filename=license_asset.original_filename,
        license_content_type=license_asset.content_type,
        agreed_to_terms_at=application.agreed_to_terms_at,
        terms_version=application.terms_version,
        phone_verified_at=application.phone_verified_at,
        status=application.status,
        review_note=application.review_note,
        reviewed_at=application.reviewed_at,
        created_at=application.created_at,
        updated_at=application.updated_at,
    )


def list_owner_applications(
    db: Session,
    *,
    application_status: str | None,
    query: str | None,
    limit: int,
    offset: int,
) -> AdminOwnerApplicationListResponse:
    filters: list[object] = []
    if application_status is not None:
        if application_status not in _APPLICATION_STATUSES:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_application_status",
                "The owner application status is not supported.",
            )
        filters.append(models.OwnerApplication.status == application_status)
    if pattern := _search_pattern(query):
        filters.append(
            or_(
                models.OwnerApplication.business_name.ilike(pattern, escape="\\"),
                models.OwnerApplication.registration_number.ilike(pattern, escape="\\"),
                models.OwnerApplication.address.ilike(pattern, escape="\\"),
            )
        )
    applications = list(
        db.scalars(
            select(models.OwnerApplication)
            .where(*filters)
            .order_by(
                models.OwnerApplication.created_at.desc(),
                models.OwnerApplication.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )
    total = int(
        db.scalar(select(func.count(models.OwnerApplication.id)).where(*filters)) or 0
    )
    return AdminOwnerApplicationListResponse(
        items=[_application_response(db, application) for application in applications],
        total=total,
    )


def review_owner_application(
    db: Session,
    admin: models.User,
    application_id: str,
    payload: AdminOwnerApplicationReview,
) -> AdminOwnerApplicationResponse:
    application = db.scalar(
        select(models.OwnerApplication)
        .where(models.OwnerApplication.id == application_id)
        .with_for_update()
    )
    if application is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "owner_application_not_found",
            "Owner application not found.",
        )
    previous_status = application.status
    if previous_status in _FINAL_APPLICATION_STATUSES:
        raise _error(
            status.HTTP_409_CONFLICT,
            "owner_application_already_reviewed",
            "A finalized owner application cannot be reviewed again.",
        )

    restaurant_id = payload.restaurant_id or application.restaurant_id
    if payload.status == "approved":
        if restaurant_id is None:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "restaurant_required",
                "Select an existing restaurant before approving this application.",
            )
        restaurant = db.scalar(
            select(models.Restaurant)
            .where(models.Restaurant.id == restaurant_id)
            .with_for_update()
        )
        if restaurant is None:
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "restaurant_not_found",
                "Restaurant not found.",
            )
        if restaurant.owner_user_id not in {None, application.applicant_user_id}:
            raise _error(
                status.HTTP_409_CONFLICT,
                "restaurant_already_claimed",
                "This restaurant already belongs to another owner.",
            )
        other_owner_id = db.scalar(
            select(models.RestaurantMembership.user_id).where(
                models.RestaurantMembership.restaurant_id == restaurant.id,
                models.RestaurantMembership.user_id != application.applicant_user_id,
                models.RestaurantMembership.role == "owner",
                models.RestaurantMembership.status == "active",
            )
        )
        if other_owner_id is not None:
            raise _error(
                status.HTTP_409_CONFLICT,
                "restaurant_already_claimed",
                "This restaurant already has another active owner.",
            )

        applicant = db.get(models.User, application.applicant_user_id)
        if applicant is None or applicant.is_guest or not applicant.is_active:
            raise _error(
                status.HTTP_409_CONFLICT,
                "invalid_owner_account",
                "The applicant account cannot receive restaurant access.",
            )
        membership = db.get(
            models.RestaurantMembership,
            {"restaurant_id": restaurant.id, "user_id": applicant.id},
        )
        if membership is None:
            db.add(
                models.RestaurantMembership(
                    restaurant_id=restaurant.id,
                    user_id=applicant.id,
                    role="owner",
                    status="active",
                )
            )
        else:
            membership.role = "owner"
            membership.status = "active"
        restaurant.owner_user_id = applicant.id
        restaurant.is_verified = True
        applicant.roles = list(dict.fromkeys([*(applicant.roles or []), "owner"]))
        application.restaurant_id = restaurant.id
        _audit(
            db,
            actor_user_id=admin.id,
            action="restaurant.owner_granted",
            resource_type="restaurant",
            resource_id=restaurant.id,
            details={
                "owner_user_id": applicant.id,
                "owner_application_id": application.id,
            },
        )

    application.status = payload.status
    application.review_note = payload.review_note
    application.reviewed_at = (
        models.utcnow() if payload.status in _FINAL_APPLICATION_STATUSES else None
    )
    _audit(
        db,
        actor_user_id=admin.id,
        action="owner_application.reviewed",
        resource_type="owner_application",
        resource_id=application.id,
        details={
            "previous_status": previous_status,
            "status": payload.status,
            "restaurant_id": application.restaurant_id,
            "has_review_note": payload.review_note is not None,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            status.HTTP_409_CONFLICT,
            "restaurant_already_claimed",
            "This restaurant already has another active owner.",
        ) from exc
    db.refresh(application)
    return _application_response(db, application)


def get_license_download(
    db: Session, admin: models.User, application_id: str
) -> tuple[models.MediaAsset, Path]:
    application = db.get(models.OwnerApplication, application_id)
    if application is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "owner_application_not_found",
            "Owner application not found.",
        )
    asset = db.get(models.MediaAsset, application.license_media_id)
    if asset is None or asset.purpose != "business_license" or asset.status != "uploaded":
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "business_license_not_found",
            "The business-license file is not available.",
        )

    upload_root = get_settings().upload_dir.expanduser().resolve()
    storage_key = Path(asset.storage_key)
    if storage_key.is_absolute() or ".." in storage_key.parts:
        raise _error(
            status.HTTP_409_CONFLICT,
            "unsafe_license_path",
            "The business-license storage path is invalid.",
        )
    license_path = (upload_root / storage_key).resolve()
    if upload_root not in license_path.parents or not license_path.is_file():
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "business_license_not_found",
            "The business-license file is not available.",
        )

    _audit(
        db,
        actor_user_id=admin.id,
        action="owner_application.license_downloaded",
        resource_type="owner_application",
        resource_id=application.id,
        details={"license_media_id": asset.id},
    )
    db.commit()
    return asset, license_path


def list_audit_events(
    db: Session, *, limit: int, offset: int
) -> AdminAuditEventListResponse:
    events = list(
        db.scalars(
            select(models.AuditEvent)
            .order_by(models.AuditEvent.created_at.desc(), models.AuditEvent.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id is not None}
    actors = {
        user.id: user.email
        for user in db.scalars(select(models.User).where(models.User.id.in_(actor_ids))).all()
    }
    return AdminAuditEventListResponse(
        items=[
            AdminAuditEventResponse(
                id=event.id,
                actor_user_id=event.actor_user_id,
                actor_email=actors.get(event.actor_user_id),
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                details=dict(event.details or {}),
                created_at=event.created_at,
            )
            for event in events
        ],
        total=_count(db, models.AuditEvent.id),
    )
