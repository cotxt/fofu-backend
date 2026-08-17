from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.dependencies import CurrentUser, DBSession, RegisteredUser
from app.schemas.owner import (
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
from app.services import owner as owner_service

router = APIRouter(prefix="/owner", tags=["owner"])


@router.post(
    "/applications",
    response_model=OwnerApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_owner_application(
    payload: OwnerApplicationCreate,
    db: DBSession,
    user: CurrentUser,
) -> OwnerApplicationResponse:
    return owner_service.create_owner_application(db, user, payload)


@router.get("/applications/status", response_model=OwnerApplicationListResponse)
def get_owner_application_status(
    db: DBSession,
    user: CurrentUser,
) -> OwnerApplicationListResponse:
    return owner_service.list_owner_applications(db, user)


@router.get("/applications/{application_id}", response_model=OwnerApplicationResponse)
def get_owner_application(
    application_id: str,
    db: DBSession,
    user: CurrentUser,
) -> OwnerApplicationResponse:
    return owner_service.get_owner_application(db, user, application_id)


@router.get("/restaurants", response_model=OwnerRestaurantListResponse)
def get_managed_restaurants(
    db: DBSession,
    user: RegisteredUser,
) -> OwnerRestaurantListResponse:
    return owner_service.list_managed_restaurants(db, user)


@router.get("/application-restaurants", response_model=OwnerRestaurantListResponse)
def get_application_restaurants(
    db: DBSession,
    _: RegisteredUser,
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> OwnerRestaurantListResponse:
    return owner_service.list_application_restaurants(db, query=q)


@router.get(
    "/restaurants/{restaurant_id}/dashboard",
    response_model=OwnerDashboardResponse,
)
def get_owner_dashboard(
    restaurant_id: str,
    db: DBSession,
    user: CurrentUser,
) -> OwnerDashboardResponse:
    return owner_service.get_dashboard(db, user, restaurant_id)


@router.patch(
    "/restaurants/{restaurant_id}/open-status",
    response_model=OwnerRestaurantSummary,
)
def patch_open_status(
    restaurant_id: str,
    payload: OpenStatusPatch,
    db: DBSession,
    user: CurrentUser,
) -> OwnerRestaurantSummary:
    return owner_service.update_open_status(db, user, restaurant_id, payload)


@router.patch(
    "/restaurants/{restaurant_id}/hours",
    response_model=list[OpeningHourResponse],
)
def patch_opening_hours(
    restaurant_id: str,
    payload: OpeningHoursPatch,
    db: DBSession,
    user: CurrentUser,
) -> list[OpeningHourResponse]:
    return owner_service.update_opening_hours(db, user, restaurant_id, payload)


@router.patch(
    "/restaurants/{restaurant_id}/menu-items/{menu_item_id}/availability",
    response_model=OwnerMenuItemResponse,
)
def patch_menu_availability(
    restaurant_id: str,
    menu_item_id: str,
    payload: MenuAvailabilityPatch,
    db: DBSession,
    user: CurrentUser,
) -> OwnerMenuItemResponse:
    return owner_service.update_menu_availability(
        db,
        user,
        restaurant_id,
        menu_item_id,
        payload,
    )
