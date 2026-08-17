from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import models
from app.database import Base
from app.schemas.cart import OrderCard
from app.seed import DEMO_USER_ID, seed_demo_data
from app.services import catalog as catalog_service
from app.services import profile as profile_service
from scripts.seed_qa import (
    QA_MENU_ITEM_IDS,
    QA_ORDER_IDS,
    QA_RESTAURANT_IDS,
    QA_REVIEW_IDS,
    install_qa_profile,
    validate_qa_environment,
)


@pytest.fixture
def qa_db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def _table_counts(db: Session) -> tuple[int, ...]:
    return tuple(
        int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            models.User,
            models.Restaurant,
            models.OpeningHour,
            models.MenuCategory,
            models.MenuItem,
            models.Review,
            models.SavedRestaurant,
            models.Order,
            models.OrderItem,
        )
    )


def test_qa_profile_is_opt_in_idempotent_and_preserves_existing_rows(
    qa_db: Session,
) -> None:
    seed_demo_data(qa_db)
    assert not qa_db.scalars(
        select(models.Restaurant).where(models.Restaurant.id.in_(QA_RESTAURANT_IDS.values()))
    ).all()

    user_owned = models.User(
        id="user-owned-outside-qa-fixtures",
        email="preserve-me@example.test",
        password_hash=None,
        display_name="Preserve Me",
        locale="en",
        is_guest=True,
        is_active=True,
        roles=[],
    )
    qa_db.add(user_owned)
    qa_db.commit()

    summary = install_qa_profile(qa_db, environment="test")
    assert summary.restaurants == 3
    assert summary.menu_items == 3
    assert summary.reviews == 2
    assert summary.saved_restaurants == 3
    assert summary.orders == 2

    restaurants = {
        restaurant.id: restaurant
        for restaurant in qa_db.scalars(
            select(models.Restaurant).where(
                models.Restaurant.id.in_(QA_RESTAURANT_IDS.values())
            )
        )
    }
    assert len(restaurants) == 3
    assert all(restaurant.name_en.startswith("QA Fictional") for restaurant in restaurants.values())
    assert all(restaurant.phone is None for restaurant in restaurants.values())
    assert (
        restaurants[QA_RESTAURANT_IDS["closed"]].is_open,
        restaurants[QA_RESTAURANT_IDS["closed"]].is_verified,
        restaurants[QA_RESTAURANT_IDS["closed"]].is_published,
    ) == (False, True, True)
    assert (
        restaurants[QA_RESTAURANT_IDS["unverified"]].is_open,
        restaurants[QA_RESTAURANT_IDS["unverified"]].is_verified,
        restaurants[QA_RESTAURANT_IDS["unverified"]].is_published,
    ) == (True, False, True)
    assert (
        restaurants[QA_RESTAURANT_IDS["draft"]].is_open,
        restaurants[QA_RESTAURANT_IDS["draft"]].is_verified,
        restaurants[QA_RESTAURANT_IDS["draft"]].is_published,
    ) == (True, True, False)

    demo_user = qa_db.get(models.User, DEMO_USER_ID)
    assert demo_user is not None
    saved = profile_service.list_saved_restaurants(
        qa_db, demo_user, cursor=None, limit=20
    )
    assert len(saved.items) == 3
    assert {item.is_open for item in saved.items} == {True, False}
    assert {item.is_verified for item in saved.items} == {True, False}

    history = profile_service.list_order_history(qa_db, demo_user, cursor=None, limit=20)
    assert len(history.items) == 2
    assert {order.serving_mode for order in history.items} == {"dine_in", "takeout"}
    assert {order.total_amount for order in history.items} == {20_000, 43_000}

    published = catalog_service.menu_item_reviews(
        qa_db,
        QA_MENU_ITEM_IDS["closed"],
        cursor=None,
        limit=20,
    )
    unpublished = catalog_service.menu_item_reviews(
        qa_db,
        QA_MENU_ITEM_IDS["unverified"],
        cursor=None,
        limit=20,
    )
    assert published.total == 1
    assert unpublished.total == 0
    assert qa_db.get(models.Review, QA_REVIEW_IDS["unpublished"]) is not None

    for order_id in QA_ORDER_IDS.values():
        order = qa_db.get(models.Order, order_id)
        assert order is not None
        assert OrderCard.model_validate(order.response_snapshot).order_id == order_id

    counts_after_first_run = _table_counts(qa_db)
    closed = restaurants[QA_RESTAURANT_IDS["closed"]]
    closed.name_en = "Locally adjusted QA label"
    user_owned.display_name = "Still Preserved"
    qa_db.commit()

    install_qa_profile(qa_db, environment="test")
    assert _table_counts(qa_db) == counts_after_first_run
    assert qa_db.get(models.Restaurant, closed.id).name_en == "Locally adjusted QA label"
    assert qa_db.get(models.User, user_owned.id).display_name == "Still Preserved"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_qa_profile_rejects_deployment_environments(environment: str) -> None:
    with pytest.raises(RuntimeError, match="restricted to local and test"):
        validate_qa_environment(environment)
