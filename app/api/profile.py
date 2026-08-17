from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.dependencies import CurrentUser, DBSession
from app.schemas.profile import (
    OrderHistoryListResponse,
    PassportPatch,
    PassportResponse,
    ProfilePatch,
    ProfileResponse,
    SavedRestaurantListResponse,
    SavedRestaurantResponse,
)
from app.services import profile as profile_service

router = APIRouter(prefix="/me", tags=["profile"])


@router.get("", response_model=ProfileResponse)
def get_me(user: CurrentUser) -> ProfileResponse:
    return profile_service.get_profile(user)


@router.patch("", response_model=ProfileResponse)
def patch_me(
    payload: ProfilePatch,
    db: DBSession,
    user: CurrentUser,
) -> ProfileResponse:
    return profile_service.update_profile(db, user, payload)


@router.get("/passport", response_model=PassportResponse)
def get_passport(db: DBSession, user: CurrentUser) -> PassportResponse:
    return profile_service.get_passport(db, user)


@router.patch("/passport", response_model=PassportResponse)
def patch_passport(
    payload: PassportPatch,
    db: DBSession,
    user: CurrentUser,
) -> PassportResponse:
    return profile_service.update_passport(db, user, payload)


@router.get("/saved-restaurants", response_model=SavedRestaurantListResponse)
def get_saved_restaurants(
    db: DBSession,
    user: CurrentUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SavedRestaurantListResponse:
    return profile_service.list_saved_restaurants(
        db,
        user,
        cursor=cursor,
        limit=limit,
    )


@router.put(
    "/saved-restaurants/{restaurant_id}",
    response_model=SavedRestaurantResponse,
)
def put_saved_restaurant(
    restaurant_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SavedRestaurantResponse:
    return profile_service.save_restaurant(db, user, restaurant_id)


@router.delete(
    "/saved-restaurants/{restaurant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_saved_restaurant(
    restaurant_id: str,
    db: DBSession,
    user: CurrentUser,
) -> Response:
    profile_service.unsave_restaurant(db, user, restaurant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/orders", response_model=OrderHistoryListResponse)
def get_order_history(
    db: DBSession,
    user: CurrentUser,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> OrderHistoryListResponse:
    return profile_service.list_order_history(db, user, cursor=cursor, limit=limit)
