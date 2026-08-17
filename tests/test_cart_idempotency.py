from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app import models
from app.schemas.cart import (
    CompatibilityAssessment,
    OrderCard,
    OrderCardItem,
)
from app.schemas.catalog import Money
from app.services import cart as cart_service


def _preview() -> OrderCard:
    price = Money(amount=16_000, currency="KRW", formatted="₩16,000")
    return OrderCard(
        status="preview",
        cart_id="cart-1",
        cart_version=2,
        menu_revision=1,
        restaurant_id="restaurant-1",
        restaurant_name="Halmoni's Table",
        table_label="A-1",
        serving_mode="dine_in",
        items=[
            OrderCardItem(
                menu_item_id="item-1",
                name="Samgyeopsal",
                original_name="삼겹살",
                quantity=1,
                unit_price=price,
                line_total=price,
            )
        ],
        subtotal=price,
        total=price,
        korean_phrase="삼겹살 1개 주세요",
        translated_phrase="Samgyeopsal × 1, please",
        compatibility=CompatibilityAssessment(
            status="unknown",
            disclaimer="Confirm allergies with restaurant staff.",
        ),
        generated_at=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
    )


def _persisted_order(fingerprint: str, snapshot: dict[str, Any]) -> models.Order:
    return models.Order(
        id="winning-order",
        user_id="user-1",
        restaurant_id="restaurant-1",
        status="prepared",
        serving_mode="dine_in",
        subtotal_amount=16_000,
        total_amount=16_000,
        currency="KRW",
        korean_phrase="삼겹살 1개 주세요",
        translated_phrase="Samgyeopsal × 1, please",
        idempotency_key="same-key-0001",
        request_fingerprint=fingerprint,
        response_snapshot=snapshot,
    )


class _CommitRaceSession:
    def __init__(self, error: IntegrityError, winner: models.Order | None) -> None:
        self.error = error
        self.winner = winner
        self.scalar_calls = 0
        self.rollback_calls = 0
        self.added: list[object] = []

    def scalar(self, _statement: object) -> models.Order | None:
        self.scalar_calls += 1
        return None if self.scalar_calls == 1 else self.winner

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        raise self.error

    def rollback(self) -> None:
        self.rollback_calls += 1


def _cart() -> SimpleNamespace:
    return SimpleNamespace(
        id="cart-1",
        user_id="user-1",
        restaurant_id="restaurant-1",
        serving_mode="dine_in",
    )


def test_unique_first_use_race_reloads_and_replays_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    preview = _preview()
    fingerprint = cart_service._order_card_request_fingerprint(
        cart_id="cart-1",
        expected_version=2,
        expected_menu_revision=1,
        locale="EN",
    )
    winning_response = preview.model_copy(
        update={"order_id": "winning-order", "status": "prepared"}
    )
    winner = _persisted_order(fingerprint, winning_response.model_dump(mode="json"))
    error = IntegrityError(
        "INSERT INTO orders",
        {},
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: orders.user_id, orders.idempotency_key"
        ),
    )
    session = _CommitRaceSession(error, winner)
    monkeypatch.setattr(cart_service, "build_order_card", lambda *_args, **_kwargs: preview)

    result = cart_service.prepare_order_card(
        session,  # type: ignore[arg-type]
        _cart(),  # type: ignore[arg-type]
        locale="en",
        idempotency_key="same-key-0001",
        expected_version=2,
        expected_menu_revision=1,
    )

    assert result.model_dump(mode="json") == winning_response.model_dump(mode="json")
    assert session.rollback_calls == 1
    assert session.scalar_calls == 2


def test_unrelated_integrity_error_is_not_treated_as_idempotency_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = _preview()
    error = IntegrityError(
        "INSERT INTO order_items",
        {},
        sqlite3.IntegrityError("NOT NULL constraint failed: order_items.quantity"),
    )
    session = _CommitRaceSession(error, None)
    monkeypatch.setattr(cart_service, "build_order_card", lambda *_args, **_kwargs: preview)

    with pytest.raises(IntegrityError) as exc_info:
        cart_service.prepare_order_card(
            session,  # type: ignore[arg-type]
            _cart(),  # type: ignore[arg-type]
            locale="en",
            idempotency_key="same-key-0001",
            expected_version=2,
            expected_menu_revision=1,
        )

    assert exc_info.value is error
    assert session.rollback_calls == 1
    assert session.scalar_calls == 1


def test_postgresql_generated_idempotency_constraint_is_recognized() -> None:
    original = sqlite3.IntegrityError("duplicate key")
    original.diag = SimpleNamespace(  # type: ignore[attr-defined]
        constraint_name="orders_user_id_idempotency_key_key"
    )
    error = IntegrityError("INSERT INTO orders", {}, original)

    assert cart_service._is_idempotency_unique_violation(error)
