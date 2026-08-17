from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.seed import (
    DEMO_USER_ID,
    HALMONI_RESTAURANT_ID,
    SECOND_REVIEWER_ID,
    _id,
    _item_id,
    _merge,
    seed_demo_data,
)

QA_SEED_VERSION = "qa-v1"
QA_BASE_TIME = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
SAFE_QA_ENVIRONMENTS = {"local", "test"}


def _qa_id(resource: str, key: str) -> str:
    return _id(f"{QA_SEED_VERSION}:{resource}:{key}")


QA_RESTAURANT_IDS = {
    "closed": _qa_id("restaurant", "closed-noodle-lab"),
    "unverified": _qa_id("restaurant", "unverified-garden"),
    "draft": _qa_id("restaurant", "hidden-draft-kitchen"),
}
QA_CATEGORY_IDS = {
    key: _qa_id("category", key) for key in QA_RESTAURANT_IDS
}
QA_MENU_ITEM_IDS = {
    "closed": _qa_id("menu-item", "pepper-noodles"),
    "unverified": _qa_id("menu-item", "garden-rice"),
    "draft": _qa_id("menu-item", "draft-tofu-soup"),
}
QA_REVIEW_IDS = {
    "published": _qa_id("review", "pepper-noodles-published"),
    "unpublished": _qa_id("review", "garden-rice-unpublished"),
}
QA_ORDER_IDS = {
    "dine_in": _qa_id("order", "halmoni-dine-in"),
    "takeout": _qa_id("order", "closed-noodle-takeout"),
}


@dataclass(frozen=True)
class QASeedSummary:
    restaurants: int = 3
    menu_items: int = 3
    reviews: int = 2
    saved_restaurants: int = 3
    orders: int = 2


_RESTAURANTS: tuple[dict[str, Any], ...] = (
    {
        "key": "closed",
        "slug": "qa-fictional-closed-noodle-lab",
        "name_en": "QA Fictional Closed Noodle Lab",
        "name_ko": "QA 가상 휴무 국수 연구소",
        "category": "QA noodles",
        "hero_style": "spice",
        "latitude": 37.5552,
        "longitude": 126.9210,
        "rating_avg": "4.0",
        "rating_count": 1,
        "is_open": False,
        "is_verified": True,
        "is_published": True,
    },
    {
        "key": "unverified",
        "slug": "qa-fictional-unverified-garden",
        "name_en": "QA Fictional Unverified Garden",
        "name_ko": "QA 가상 미검증 정원",
        "category": "QA plant-based",
        "hero_style": "garden",
        "latitude": 37.5591,
        "longitude": 126.9254,
        "rating_avg": "0.0",
        "rating_count": 0,
        "is_open": True,
        "is_verified": False,
        "is_published": True,
    },
    {
        "key": "draft",
        "slug": "qa-fictional-hidden-draft-kitchen",
        "name_en": "QA Fictional Hidden Draft Kitchen",
        "name_ko": "QA 가상 비공개 초안 주방",
        "category": "QA draft",
        "hero_style": "charcoal",
        "latitude": 37.5528,
        "longitude": 126.9245,
        "rating_avg": "0.0",
        "rating_count": 0,
        "is_open": True,
        "is_verified": True,
        "is_published": False,
    },
)

_MENU_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "key": "closed",
        "slug": "qa-pepper-noodles",
        "name_en": "QA Pepper Noodles",
        "name_ko": "QA 후추 국수",
        "price": 10_000,
        "spice": 2,
        "badge": "qa-fixture",
        "ingredients": ("sweet-potato-noodle", "scallion", "gochujang"),
        "allergens": (("gluten", "contains"),),
        "claims": (),
        "is_available": True,
    },
    {
        "key": "unverified",
        "slug": "qa-garden-rice",
        "name_en": "QA Garden Rice",
        "name_ko": "QA 정원 덮밥",
        "price": 12_500,
        "spice": 0,
        "badge": "plant-based",
        "ingredients": ("rice", "mushroom", "spinach"),
        "allergens": (("soy", "may_contain"),),
        "claims": ("vegan", "vegetarian"),
        "is_available": True,
    },
    {
        "key": "draft",
        "slug": "qa-draft-tofu-soup",
        "name_en": "QA Draft Tofu Soup",
        "name_ko": "QA 초안 두부국",
        "price": 9_500,
        "spice": 1,
        "badge": None,
        "ingredients": ("tofu", "scallion", "soy"),
        "allergens": (("soy", "contains"),),
        "claims": ("vegetarian",),
        "is_available": False,
    },
)


def validate_qa_environment(environment: str) -> None:
    if environment not in SAFE_QA_ENVIRONMENTS:
        raise RuntimeError(
            "The fictional QA seed is restricted to local and test environments."
        )


def _seed_qa_restaurants(db: Session) -> None:
    for index, spec in enumerate(_RESTAURANTS):
        key = str(spec["key"])
        restaurant_id = QA_RESTAURANT_IDS[key]
        fixture_time = QA_BASE_TIME + timedelta(minutes=index)
        _merge(
            db,
            models.Restaurant(
                id=restaurant_id,
                slug=spec["slug"],
                owner_user_id=None,
                name_en=spec["name_en"],
                name_ko=spec["name_ko"],
                description_en=(
                    "A fictional venue created only for Fofu QA. It is not a real business."
                ),
                description_ko="Fofu QA 전용 가상 식당이며 실제 사업장이 아닙니다.",
                handle=f"@{spec['slug']}",
                category=spec["category"],
                hero_style=spec["hero_style"],
                address_en=f"Fictional QA Block {index + 1}, Seoul (not a real address)",
                address_ko=f"서울 QA 테스트 전용 가상 주소 {index + 1}",
                phone=None,
                latitude=spec["latitude"],
                longitude=spec["longitude"],
                currency="KRW",
                timezone_name="Asia/Seoul",
                rating_avg=Decimal(spec["rating_avg"]),
                rating_count=spec["rating_count"],
                is_verified=spec["is_verified"],
                is_open=spec["is_open"],
                is_published=spec["is_published"],
                menu_revision=1,
                cover_image_url=None,
                gallery=[],
                created_at=fixture_time,
                updated_at=fixture_time,
            ),
        )
        for day in range(7):
            is_closed = key == "closed"
            _merge(
                db,
                models.OpeningHour(
                    id=_qa_id("opening-hour", f"{key}:{day}"),
                    restaurant_id=restaurant_id,
                    day_of_week=day,
                    opens_at=None if is_closed else time(10, 0),
                    closes_at=None if is_closed else time(20, 0),
                    is_closed=is_closed,
                ),
            )
    db.flush()


def _seed_qa_menu(db: Session) -> None:
    for index, spec in enumerate(_MENU_ITEMS):
        key = str(spec["key"])
        fixture_time = QA_BASE_TIME + timedelta(hours=1, minutes=index)
        category_id = QA_CATEGORY_IDS[key]
        restaurant_id = QA_RESTAURANT_IDS[key]
        item_id = QA_MENU_ITEM_IDS[key]
        _merge(
            db,
            models.MenuCategory(
                id=category_id,
                restaurant_id=restaurant_id,
                slug="qa-fixtures",
                name_en="QA fixtures",
                name_ko="QA 테스트 메뉴",
                sort_order=0,
                is_active=True,
                created_at=fixture_time,
                updated_at=fixture_time,
            ),
        )
        _merge(
            db,
            models.MenuItem(
                id=item_id,
                restaurant_id=restaurant_id,
                category_id=category_id,
                slug=spec["slug"],
                name_en=spec["name_en"],
                name_ko=spec["name_ko"],
                pronunciation=None,
                description_en="A fictional menu item used only for automated QA.",
                description_ko="자동 QA에서만 사용하는 가상 메뉴입니다.",
                price_amount=spec["price"],
                currency="KRW",
                serving_description="One QA serving",
                spice_level=spec["spice"],
                taste_profile={"qa": 1.0},
                local_tips=[],
                badge=spec["badge"],
                image_url=None,
                media=[],
                is_available=spec["is_available"],
                sort_order=0,
                created_at=fixture_time,
                updated_at=fixture_time,
            ),
        )
        db.flush()

        for sort_order, ingredient_code in enumerate(spec["ingredients"]):
            _merge(
                db,
                models.MenuItemIngredient(
                    menu_item_id=item_id,
                    ingredient_code=ingredient_code,
                    detail_en=None,
                    detail_ko=None,
                    is_primary=sort_order == 0,
                    sort_order=sort_order,
                ),
            )
        for allergen_code, relation_type in spec["allergens"]:
            _merge(
                db,
                models.MenuItemAllergen(
                    menu_item_id=item_id,
                    allergen_code=allergen_code,
                    relation_type=relation_type,
                    verification_status="qa_fixture",
                    source="fictional QA fixture",
                    verified_at=QA_BASE_TIME,
                ),
            )
        declared_allergens = {code for code, _ in spec["allergens"]}
        for allergen_code in ("peanut", "milk"):
            if allergen_code in declared_allergens:
                continue
            _merge(
                db,
                models.MenuItemAllergen(
                    menu_item_id=item_id,
                    allergen_code=allergen_code,
                    relation_type="free_from",
                    verification_status="qa_fixture",
                    source="fictional QA fixture",
                    verified_at=QA_BASE_TIME,
                ),
            )
        for claim in spec["claims"]:
            _merge(
                db,
                models.MenuItemDietaryClaim(
                    menu_item_id=item_id,
                    code=claim,
                    verification_status="qa_fixture",
                ),
            )
    db.flush()


def _seed_qa_reviews(db: Session) -> None:
    reviews = (
        models.Review(
            id=QA_REVIEW_IDS["published"],
            menu_item_id=QA_MENU_ITEM_IDS["closed"],
            author_user_id=DEMO_USER_ID,
            rating=4,
            body=(
                "Warm and peppery with a good chew. "
                "This is a fictional review created only for QA."
            ),
            author_display_name="Mia Laurent",
            author_country_code="FR",
            tags=["qa-fixture", "noodles"],
            is_published=True,
            created_at=QA_BASE_TIME + timedelta(days=1),
            updated_at=QA_BASE_TIME + timedelta(days=1),
        ),
        models.Review(
            id=QA_REVIEW_IDS["unpublished"],
            menu_item_id=QA_MENU_ITEM_IDS["unverified"],
            author_user_id=SECOND_REVIEWER_ID,
            rating=3,
            body="Fictional moderation fixture. This review must stay out of public results.",
            author_display_name="Julien Moreau",
            author_country_code="FR",
            tags=["qa-fixture", "moderation"],
            is_published=False,
            created_at=QA_BASE_TIME + timedelta(days=1, minutes=1),
            updated_at=QA_BASE_TIME + timedelta(days=1, minutes=1),
        ),
    )
    for review in reviews:
        _merge(db, review)
    db.flush()


def _seed_qa_saved_restaurants(db: Session) -> None:
    restaurant_ids = (
        HALMONI_RESTAURANT_ID,
        QA_RESTAURANT_IDS["closed"],
        QA_RESTAURANT_IDS["unverified"],
    )
    for index, restaurant_id in enumerate(restaurant_ids):
        _merge(
            db,
            models.SavedRestaurant(
                user_id=DEMO_USER_ID,
                restaurant_id=restaurant_id,
                created_at=QA_BASE_TIME + timedelta(days=2, minutes=index),
            ),
        )
    db.flush()


def _money(amount: int) -> dict[str, Any]:
    return {"amount": amount, "currency": "KRW", "formatted": f"₩{amount:,}"}


def _order_snapshot(
    *,
    order_id: str,
    restaurant_id: str,
    restaurant_name: str,
    serving_mode: str,
    table_label: str | None,
    lines: tuple[dict[str, Any], ...],
    korean_phrase: str,
    translated_phrase: str,
    generated_at: datetime,
) -> dict[str, Any]:
    total = sum(int(line["unit_price"]) * int(line["quantity"]) for line in lines)
    return {
        "order_id": order_id,
        "status": "prepared",
        "cart_id": _qa_id("cart-snapshot", order_id),
        "cart_version": 1,
        "menu_revision": 1,
        "restaurant_id": restaurant_id,
        "restaurant_name": restaurant_name,
        "table_label": table_label,
        "serving_mode": serving_mode,
        "items": [
            {
                "menu_item_id": line["menu_item_id"],
                "name": line["name_en"],
                "original_name": line["name_ko"],
                "quantity": line["quantity"],
                "unit_price": _money(int(line["unit_price"])),
                "line_total": _money(int(line["unit_price"]) * int(line["quantity"])),
                "notes": line.get("notes"),
                "request_codes": [],
            }
            for line in lines
        ],
        "subtotal": _money(total),
        "total": _money(total),
        "korean_phrase": korean_phrase,
        "translated_phrase": translated_phrase,
        "request_note_ko": None,
        "request_note_localized": None,
        "compatibility": {
            "status": "unknown",
            "reasons": [],
            "disclaimer": "Fictional QA order; confirm real allergies with restaurant staff.",
        },
        "generated_at": generated_at.isoformat(),
    }


def _seed_order(
    db: Session,
    *,
    key: str,
    restaurant_id: str,
    restaurant_name: str,
    serving_mode: str,
    table_label: str | None,
    lines: tuple[dict[str, Any], ...],
    korean_phrase: str,
    translated_phrase: str,
    created_at: datetime,
) -> None:
    order_id = QA_ORDER_IDS[key]
    total = sum(int(line["unit_price"]) * int(line["quantity"]) for line in lines)
    idempotency_key = f"{QA_SEED_VERSION}-{key}-order"
    snapshot = _order_snapshot(
        order_id=order_id,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        serving_mode=serving_mode,
        table_label=table_label,
        lines=lines,
        korean_phrase=korean_phrase,
        translated_phrase=translated_phrase,
        generated_at=created_at,
    )
    _merge(
        db,
        models.Order(
            id=order_id,
            user_id=DEMO_USER_ID,
            restaurant_id=restaurant_id,
            status="prepared",
            serving_mode=serving_mode,
            subtotal_amount=total,
            total_amount=total,
            currency="KRW",
            korean_phrase=korean_phrase,
            translated_phrase=translated_phrase,
            allergy_note_ko=None,
            allergy_note_localized=None,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(idempotency_key.encode()).hexdigest(),
            response_snapshot=snapshot,
            created_at=created_at,
            updated_at=created_at,
        ),
    )
    db.flush()
    for index, line in enumerate(lines):
        _merge(
            db,
            models.OrderItem(
                id=_qa_id("order-item", f"{key}:{index}"),
                order_id=order_id,
                menu_item_id=line["menu_item_id"],
                name_en_snapshot=line["name_en"],
                name_ko_snapshot=line["name_ko"],
                unit_price_amount=line["unit_price"],
                quantity=line["quantity"],
                notes=line.get("notes"),
            ),
        )


def _seed_qa_orders(db: Session) -> None:
    _seed_order(
        db,
        key="dine_in",
        restaurant_id=HALMONI_RESTAURANT_ID,
        restaurant_name="Halmoni's Table",
        serving_mode="dine_in",
        table_label="QA-7",
        lines=(
            {
                "menu_item_id": _item_id("halmonis-table", "samgyeopsal"),
                "name_en": "Samgyeopsal",
                "name_ko": "삼겹살",
                "unit_price": 16_000,
                "quantity": 2,
            },
            {
                "menu_item_id": _item_id("halmonis-table", "kimchi-jjigae"),
                "name_en": "Kimchi Jjigae",
                "name_ko": "김치찌개",
                "unit_price": 11_000,
                "quantity": 1,
            },
        ),
        korean_phrase="삼겹살 2개, 김치찌개 1개 주세요",
        translated_phrase="Samgyeopsal × 2, Kimchi Jjigae × 1, please",
        created_at=QA_BASE_TIME + timedelta(days=2, hours=2),
    )
    _seed_order(
        db,
        key="takeout",
        restaurant_id=QA_RESTAURANT_IDS["closed"],
        restaurant_name="QA Fictional Closed Noodle Lab",
        serving_mode="takeout",
        table_label=None,
        lines=(
            {
                "menu_item_id": QA_MENU_ITEM_IDS["closed"],
                "name_en": "QA Pepper Noodles",
                "name_ko": "QA 후추 국수",
                "unit_price": 10_000,
                "quantity": 2,
                "notes": "Fictional QA fixture",
            },
        ),
        korean_phrase="QA 후추 국수 2개 포장해 주세요",
        translated_phrase="QA Pepper Noodles × 2 for takeout, please",
        created_at=QA_BASE_TIME + timedelta(days=3, hours=2),
    )
    db.flush()


def seed_qa_data(db: Session, *, environment: str) -> QASeedSummary:
    """Insert the deterministic fictional QA profile without updating existing rows."""

    validate_qa_environment(environment)
    try:
        _seed_qa_restaurants(db)
        _seed_qa_menu(db)
        _seed_qa_reviews(db)
        _seed_qa_saved_restaurants(db)
        _seed_qa_orders(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return QASeedSummary()


def install_qa_profile(db: Session, *, environment: str) -> QASeedSummary:
    """Ensure core demo prerequisites, then install the opt-in fictional QA rows."""

    validate_qa_environment(environment)
    seed_demo_data(db)
    return seed_qa_data(db, environment=environment)


def main() -> int:
    settings = get_settings()
    try:
        with SessionLocal() as db:
            summary = install_qa_profile(db, environment=settings.environment)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Error: QA seeding failed. Check the target database and run migrations first.",
            file=sys.stderr,
        )
        return 1

    print(f"Ensured fictional {QA_SEED_VERSION} fixtures in the configured database.")
    print(
        "Fixtures: "
        f"{summary.restaurants} restaurants, "
        f"{summary.menu_items} menu items, "
        f"{summary.reviews} reviews, "
        f"{summary.saved_restaurants} saved places, "
        f"{summary.orders} orders."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
