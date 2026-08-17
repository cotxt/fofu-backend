from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import scripts.import_jeju_catalog as region_import_module
from app import models
from app.database import Base
from scripts.import_jeju_catalog import (
    JejuImportError,
    import_jeju_catalog,
    menu_category_id_for,
    menu_item_id_for,
    restaurant_handle_for,
    restaurant_id_for,
    restaurant_slug_for,
)
from scripts.import_region_catalog import (
    import_region_catalog,
    menu_item_id_for_region,
    restaurant_handle_for_region,
    restaurant_id_for_region,
    restaurant_slug_for_region,
)


@pytest.fixture
def catalog_db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def jeju_sources(tmp_path: Path) -> tuple[Path, Path]:
    places_path = tmp_path / "jeju_full.sqlite3"
    menus_path = tmp_path / "menus_jeju.sqlite3"
    _create_places_database(places_path)
    _create_menus_database(menus_path)
    return places_path, menus_path


def _create_places_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE places (
                kakao_place_id TEXT PRIMARY KEY,
                place_name TEXT NOT NULL,
                category_name TEXT NOT NULL,
                category_group_code TEXT NOT NULL,
                phone TEXT NOT NULL,
                address_name TEXT NOT NULL,
                road_address_name TEXT NOT NULL,
                longitude REAL NOT NULL,
                latitude REAL NOT NULL
            );
            CREATE TABLE discoveries (
                kakao_place_id TEXT NOT NULL,
                region TEXT NOT NULL,
                in_region INTEGER NOT NULL,
                PRIMARY KEY (kakao_place_id, region),
                FOREIGN KEY (kakao_place_id) REFERENCES places(kakao_place_id)
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO places (
                kakao_place_id, place_name, category_name, category_group_code,
                phone, address_name, road_address_name, longitude, latitude
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "101",
                    "첫 제주식당",
                    "음식점 > 한식 > 국수",
                    "FD6",
                    "064-111-1111",
                    "제주특별자치도 제주시 구주소 1",
                    "제주특별자치도 제주시 새주소 1",
                    126.51,
                    33.50,
                ),
                (
                    "202",
                    "둘 제주카페",
                    "음식점 > 카페",
                    "CE7",
                    "",
                    "제주특별자치도 서귀포시 구주소 2",
                    "",
                    126.56,
                    33.25,
                ),
                (
                    "303",
                    "메뉴 없는 식당",
                    "음식점 > 한식",
                    "FD6",
                    "",
                    "제주특별자치도 제주시 구주소 3",
                    "제주특별자치도 제주시 새주소 3",
                    126.48,
                    33.48,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO discoveries (kakao_place_id, region, in_region)
            VALUES (?, ?, ?)
            """,
            [
                (place_id, region, 1)
                for place_id in ("101", "202", "303")
                for region in ("gyeonggi", "jeju", "seoul")
            ],
        )


def _create_menus_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE crawl_jobs (
                kakao_place_id TEXT PRIMARY KEY
            );
            CREATE TABLE menu_groups (
                kakao_place_id TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (kakao_place_id, source),
                FOREIGN KEY (kakao_place_id) REFERENCES crawl_jobs(kakao_place_id)
            );
            CREATE TABLE menu_items (
                kakao_place_id TEXT NOT NULL,
                source TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                product_id TEXT,
                name TEXT NOT NULL,
                price INTEGER,
                description TEXT,
                photo_url TEXT,
                is_recommend INTEGER,
                sold_out INTEGER,
                PRIMARY KEY (kakao_place_id, source, ordinal),
                FOREIGN KEY (kakao_place_id, source)
                    REFERENCES menu_groups(kakao_place_id, source)
            );
            """
        )
        connection.executemany(
            "INSERT INTO crawl_jobs (kakao_place_id) VALUES (?)",
            [("101",), ("202",), ("303",)],
        )
        connection.executemany(
            "INSERT INTO menu_groups (kakao_place_id, source) VALUES (?, ?)",
            [
                ("101", "menus"),
                ("101", "yogiyo_menus"),
                ("101", "yogiyo_pickup_menus"),
                ("202", "menus"),
                ("202", "yogiyo_menus"),
                ("202", "yogiyo_pickup_menus"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO menu_items (
                kakao_place_id, source, ordinal, product_id, name, price,
                description, photo_url, is_recommend, sold_out
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "101",
                    "menus",
                    0,
                    "11",
                    "고기국수",
                    9_000,
                    "진한 국물",
                    "http://images.example.test/noodle.jpg",
                    1,
                    None,
                ),
                (
                    "101",
                    "menus",
                    1,
                    "11",
                    "중복 고기국수",
                    9_500,
                    "같은 상품 ID",
                    "http://images.example.test/duplicate.jpg",
                    0,
                    None,
                ),
                (
                    "101",
                    "menus",
                    2,
                    "12",
                    "변동가 회",
                    None,
                    None,
                    "http://images.example.test/market-price.jpg",
                    0,
                    None,
                ),
                (
                    "101",
                    "menus",
                    3,
                    "13",
                    "잘못된 0원 메뉴",
                    0,
                    None,
                    None,
                    0,
                    None,
                ),
                (
                    "101",
                    "menus",
                    4,
                    "14",
                    "잘못된 고가 메뉴",
                    999_999,
                    None,
                    None,
                    0,
                    None,
                ),
                (
                    "101",
                    "yogiyo_menus",
                    0,
                    "91",
                    "배달 소스 메뉴",
                    12_000,
                    None,
                    "http://images.example.test/delivery.jpg",
                    0,
                    0,
                ),
                (
                    "101",
                    "yogiyo_pickup_menus",
                    0,
                    "92",
                    "픽업 제외 메뉴",
                    13_000,
                    None,
                    "http://images.example.test/pickup.jpg",
                    0,
                    0,
                ),
                (
                    "202",
                    "yogiyo_menus",
                    0,
                    "21",
                    "한라봉차",
                    7_000,
                    "따뜻한 차",
                    "//images.example.test/tea.jpg",
                    0,
                    1,
                ),
                (
                    "202",
                    "yogiyo_pickup_menus",
                    0,
                    "22",
                    "픽업 한라봉차",
                    6_000,
                    None,
                    "http://images.example.test/pickup-tea.jpg",
                    0,
                    0,
                ),
            ],
        )


def _count(db: Session, model: type[object]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar_state(path: Path) -> dict[str, tuple[bool, int | None]]:
    return {
        suffix: (sidecar.exists(), sidecar.stat().st_size if sidecar.exists() else None)
        for suffix in ("-wal", "-shm", "-journal")
        if (sidecar := Path(f"{path}{suffix}"))
    }


def _unrelated_restaurant(identifier: str, *, handle: str | None = None) -> models.Restaurant:
    return models.Restaurant(
        id=identifier,
        slug=f"unrelated-{identifier}",
        owner_user_id=None,
        name_en="Unrelated",
        name_ko="무관한 식당",
        description_en="",
        description_ko=None,
        handle=handle or f"@unrelated.{identifier}",
        category="Test",
        hero_style="charcoal",
        address_en="Unrelated address",
        address_ko="무관한 주소",
        phone=None,
        latitude=37.5,
        longitude=126.9,
        currency="KRW",
        timezone_name="Asia/Seoul",
        rating_avg=Decimal("0.0"),
        rating_count=0,
        is_verified=False,
        is_open=True,
        is_published=True,
        menu_revision=1,
        cover_image_url=None,
        gallery=[],
    )


def test_default_dry_run_validates_readonly_sources_and_rolls_back(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    places_path, menus_path = jeju_sources
    before = {
        path: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            _digest(path),
            _sidecar_state(path),
        )
        for path in jeju_sources
    }

    summary = import_jeju_catalog(
        catalog_db,
        places_path,
        menus_path,
        environment="test",
    )

    assert summary.source_places == 3
    assert summary.resources["restaurants"].inserted == 3
    assert summary.resources["menu_items"].inserted == 5
    assert _count(catalog_db, models.Restaurant) == 0
    assert _count(catalog_db, models.MenuCategory) == 0
    assert _count(catalog_db, models.MenuItem) == 0
    assert {
        path: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            _digest(path),
            _sidecar_state(path),
        )
        for path in jeju_sources
    } == before


def test_apply_maps_all_places_and_uses_menu_source_priority(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    summary = import_jeju_catalog(
        catalog_db,
        *jeju_sources,
        environment="test",
        apply=True,
        batch_size=1,
    )

    assert _count(catalog_db, models.Restaurant) == 3
    assert _count(catalog_db, models.MenuCategory) == 2
    assert _count(catalog_db, models.MenuItem) == 5
    assert summary.chosen_menu_places == 2
    assert summary.selected_menu_rows == 6
    assert summary.imported_unknown_price == 3
    assert summary.normalized_invalid_price == 2
    assert summary.skipped_invalid_price == 0
    assert summary.as_dict()["menu_items"]["skipped_invalid_price"] == 0
    assert summary.deduplicated_menu_items == 1
    assert summary.imported_menu_items == 5

    restaurant = catalog_db.get(models.Restaurant, restaurant_id_for("101"))
    fallback = catalog_db.get(models.Restaurant, restaurant_id_for("202"))
    no_menu = catalog_db.get(models.Restaurant, restaurant_id_for("303"))
    assert restaurant is not None
    assert fallback is not None
    assert no_menu is not None
    assert restaurant.slug == restaurant_slug_for("101")
    assert restaurant.handle == restaurant_handle_for("101")
    assert restaurant.name_en == restaurant.name_ko == "첫 제주식당"
    assert restaurant.address_en == restaurant.address_ko == "제주특별자치도 제주시 새주소 1"
    assert restaurant.category == "음식점 > 한식 > 국수"
    assert restaurant.is_published is True
    assert restaurant.is_verified is False
    assert restaurant.cover_image_url == "https://images.example.test/noodle.jpg"
    assert fallback.address_en == "제주특별자치도 서귀포시 구주소 2"
    assert fallback.phone is None
    assert fallback.cover_image_url == "https://images.example.test/tea.jpg"
    assert no_menu.is_published is True

    item = catalog_db.get(models.MenuItem, menu_item_id_for("101", "11"))
    fallback_item = catalog_db.get(models.MenuItem, menu_item_id_for("202", "21"))
    assert item is not None
    assert fallback_item is not None
    assert item.slug == "kakao-11"
    assert item.name_en == item.name_ko == "고기국수"
    assert item.description_en == item.description_ko == "진한 국물"
    assert item.price_amount == 9_000
    assert item.image_url == "https://images.example.test/noodle.jpg"
    assert item.badge == "Recommended"
    assert fallback_item.is_available is False
    assert fallback_item.image_url == "https://images.example.test/tea.jpg"
    unknown_price = catalog_db.get(models.MenuItem, menu_item_id_for("101", "12"))
    assert unknown_price is not None
    assert unknown_price.price_amount is None
    zero_price = catalog_db.get(models.MenuItem, menu_item_id_for("101", "13"))
    excessive_price = catalog_db.get(models.MenuItem, menu_item_id_for("101", "14"))
    assert zero_price is not None
    assert excessive_price is not None
    assert zero_price.price_amount is None
    assert excessive_price.price_amount is None
    assert catalog_db.scalar(
        select(models.MenuItem).where(models.MenuItem.name_en == "배달 소스 메뉴")
    ) is None


def test_apply_is_idempotent_and_preserves_existing_rows_and_relationships(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    first = import_jeju_catalog(
        catalog_db, *jeju_sources, environment="test", apply=True
    )
    restaurant = catalog_db.get(models.Restaurant, restaurant_id_for("101"))
    assert restaurant is not None
    restaurant_id = restaurant.id
    imported_item_id = menu_item_id_for("101", "11")
    original_revision = restaurant.menu_revision

    user = models.User(
        id="jeju-owner",
        email="jeju-owner@example.test",
        password_hash=None,
        display_name="Jeju Owner",
        locale="ko",
        is_guest=False,
        is_active=True,
        roles=["owner"],
    )
    unrelated = _unrelated_restaurant("kept")
    custom_category = models.MenuCategory(
        id="custom-category",
        restaurant_id=restaurant_id,
        slug="owner-menu",
        name_en="Owner menu",
        name_ko="점주 메뉴",
        sort_order=10,
        is_active=True,
    )
    custom_item = models.MenuItem(
        id="custom-item",
        restaurant_id=restaurant_id,
        category_id=custom_category.id,
        slug="owner-item",
        name_en="Owner item",
        name_ko="점주 메뉴",
        description_en="",
        description_ko=None,
        price_amount=1_000,
        currency="KRW",
        spice_level=0,
        taste_profile={},
        local_tips=[],
        media=[],
        is_available=True,
        sort_order=0,
    )
    catalog_db.add_all([user, unrelated, custom_category, custom_item])
    restaurant.owner_user_id = user.id
    restaurant.rating_avg = Decimal("4.9")
    restaurant.rating_count = 12
    restaurant.is_open = False
    catalog_db.add(models.SavedRestaurant(user_id=user.id, restaurant_id=restaurant_id))
    catalog_db.commit()

    second = import_jeju_catalog(
        catalog_db, *jeju_sources, environment="test", apply=True
    )
    catalog_db.expire_all()

    preserved = catalog_db.get(models.Restaurant, restaurant_id)
    assert preserved is not None
    assert first.resources["restaurants"].inserted == 3
    assert second.resources["restaurants"].unchanged == 3
    assert second.resources["menu_categories"].unchanged == 2
    assert second.resources["menu_items"].unchanged == 5
    assert preserved.owner_user_id == user.id
    assert preserved.rating_avg == Decimal("4.9")
    assert preserved.rating_count == 12
    assert preserved.is_open is False
    assert preserved.menu_revision == original_revision
    assert catalog_db.get(models.Restaurant, unrelated.id) is not None
    assert catalog_db.get(models.MenuCategory, custom_category.id) is not None
    assert catalog_db.get(models.MenuItem, custom_item.id) is not None
    assert catalog_db.get(models.MenuItem, imported_item_id) is not None
    assert catalog_db.get(models.SavedRestaurant, (user.id, restaurant_id)) is not None


def test_changed_source_updates_stable_item_and_increments_revision_once(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    import_jeju_catalog(catalog_db, *jeju_sources, environment="test", apply=True)
    item_id = menu_item_id_for("101", "11")
    restaurant_id = restaurant_id_for("101")
    before = catalog_db.get(models.Restaurant, restaurant_id)
    assert before is not None
    old_revision = before.menu_revision

    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute(
            """
            UPDATE menu_items SET price = 10000
            WHERE kakao_place_id = '101' AND source = 'menus' AND ordinal = 0
            """
        )

    summary = import_jeju_catalog(
        catalog_db, *jeju_sources, environment="test", apply=True
    )
    catalog_db.expire_all()

    item = catalog_db.get(models.MenuItem, item_id)
    restaurant = catalog_db.get(models.Restaurant, restaurant_id)
    assert item is not None
    assert restaurant is not None
    assert item.price_amount == 10_000
    assert item.id == item_id
    assert summary.resources["menu_items"].updated == 1
    assert restaurant.menu_revision == old_revision + 1


def test_known_price_becoming_unknown_updates_in_place(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    import_jeju_catalog(catalog_db, *jeju_sources, environment="test", apply=True)
    item_id = menu_item_id_for("101", "11")
    restaurant_id = restaurant_id_for("101")
    before = catalog_db.get(models.Restaurant, restaurant_id)
    assert before is not None
    old_revision = before.menu_revision

    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute(
            """
            UPDATE menu_items SET price = NULL
            WHERE kakao_place_id = '101'
              AND source = 'menus'
              AND product_id = '11'
            """
        )

    summary = import_jeju_catalog(
        catalog_db,
        *jeju_sources,
        environment="test",
        apply=True,
    )
    catalog_db.expire_all()

    item = catalog_db.get(models.MenuItem, item_id)
    restaurant = catalog_db.get(models.Restaurant, restaurant_id)
    assert item is not None
    assert restaurant is not None
    assert item.price_amount is None
    assert summary.resources["menu_items"].updated == 1
    assert restaurant.menu_revision == old_revision + 1


def test_invalid_price_on_reimport_becomes_unknown_without_deleting_menu_item(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    import_jeju_catalog(catalog_db, *jeju_sources, environment="test", apply=True)
    item_id = menu_item_id_for("101", "11")

    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute(
            """
            UPDATE menu_items SET price = 999999
            WHERE kakao_place_id = '101'
              AND source = 'menus'
              AND product_id = '11'
            """
        )

    summary = import_jeju_catalog(
        catalog_db,
        *jeju_sources,
        environment="test",
        apply=True,
    )
    catalog_db.expire_all()

    preserved = catalog_db.get(models.MenuItem, item_id)
    assert preserved is not None
    assert preserved.price_amount is None
    assert summary.normalized_invalid_price == 3
    assert summary.resources["menu_items"].updated == 1


@pytest.mark.parametrize("invalid_price", [0, -1_000, 500_001, "가격문의", 12.5])
def test_invalid_price_shapes_are_imported_as_unknown(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    invalid_price: object,
) -> None:
    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute(
            """
            UPDATE menu_items SET price = ?
            WHERE kakao_place_id = '202'
              AND source = 'yogiyo_menus'
              AND product_id = '21'
            """,
            (invalid_price,),
        )

    summary = import_jeju_catalog(
        catalog_db,
        *jeju_sources,
        environment="test",
        apply=True,
    )
    imported = catalog_db.get(models.MenuItem, menu_item_id_for("202", "21"))

    assert imported is not None
    assert imported.name_ko == "한라봉차"
    assert imported.price_amount is None
    assert summary.imported_unknown_price == 4
    assert summary.normalized_invalid_price == 3


def test_cover_image_falls_back_without_changing_menu_source_priority(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute(
            """
            UPDATE menu_items SET photo_url = NULL
            WHERE kakao_place_id = '101' AND source = 'menus'
            """
        )

    import_region_catalog(
        catalog_db,
        *jeju_sources,
        region="seoul",
        environment="test",
        apply=True,
    )

    restaurant = catalog_db.get(
        models.Restaurant,
        restaurant_id_for_region("seoul", "101"),
    )
    item = catalog_db.get(
        models.MenuItem,
        menu_item_id_for_region("seoul", "101", "11"),
    )
    assert restaurant is not None
    assert item is not None
    assert restaurant.cover_image_url == "https://images.example.test/delivery.jpg"
    assert item.name_en == "고기국수"
    assert item.image_url is None
    assert (
        catalog_db.get(models.MenuItem, menu_item_id_for_region("seoul", "101", "91"))
        is None
    )


def test_target_collision_rolls_back_earlier_batches(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    collision = _unrelated_restaurant("collision", handle=restaurant_handle_for("202"))
    catalog_db.add(collision)
    catalog_db.commit()

    with pytest.raises(JejuImportError, match="restaurant handle"):
        import_jeju_catalog(
            catalog_db,
            *jeju_sources,
            environment="test",
            apply=True,
            batch_size=1,
        )

    assert _count(catalog_db, models.Restaurant) == 1
    assert catalog_db.get(models.Restaurant, collision.id) is not None
    assert catalog_db.get(models.Restaurant, restaurant_id_for("101")) is None


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_import_refuses_deployment_environments(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    environment: str,
) -> None:
    with pytest.raises(JejuImportError, match="restricted to local and test"):
        import_jeju_catalog(
            catalog_db,
            *jeju_sources,
            environment=environment,
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0


def test_invalid_source_schema_leaves_target_empty(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute("ALTER TABLE menu_items DROP COLUMN photo_url")

    with pytest.raises(JejuImportError, match="missing columns: photo_url"):
        import_jeju_catalog(
            catalog_db,
            *jeju_sources,
            environment="test",
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0


def test_deterministic_ids_use_kakao_keys() -> None:
    assert restaurant_id_for("101") == "353e2cb8-4b3e-5a4b-9045-bc1fd26e59d6"
    assert restaurant_id_for("101") != restaurant_id_for("102")
    assert menu_category_id_for("101") == "0817cd9b-9520-56f7-80ca-85f06e037e4e"
    assert menu_item_id_for("101", "11") == "2d83c0de-5e96-5841-aaac-f902fc7cbe38"
    assert menu_item_id_for("101", "11") != menu_item_id_for("101", "12")


def test_gyeonggi_region_key_is_normalized_and_namespaced() -> None:
    gyeonggi_id = restaurant_id_for_region("gyeonggi", "101")
    assert restaurant_id_for_region(" GYEONGGI ", "101") == gyeonggi_id
    assert gyeonggi_id not in {
        restaurant_id_for_region("jeju", "101"),
        restaurant_id_for_region("seoul", "101"),
    }
    assert restaurant_slug_for_region(" GYEONGGI ", "101") == "gyeonggi-kakao-101"
    assert restaurant_handle_for_region(" GYEONGGI ", "101") == "@gyeonggi.101"


@pytest.mark.parametrize("region", ["gyeonggi", "seoul"])
def test_regional_dry_run_imports_only_in_region_places(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    region: str,
) -> None:
    with sqlite3.connect(jeju_sources[0]) as source:
        source.execute(
            """
            UPDATE discoveries SET in_region = 0
            WHERE kakao_place_id = '303' AND region = ?
            """,
            (region,),
        )
    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute("DELETE FROM crawl_jobs WHERE kakao_place_id = '303'")
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _digest(path))
        for path in jeju_sources
    }

    summary = import_region_catalog(
        catalog_db,
        *jeju_sources,
        region=region,
        environment="test",
    )

    assert summary.region == region
    assert summary.source_places == 2
    assert summary.chosen_menu_places == 2
    assert _count(catalog_db, models.Restaurant) == 0
    assert {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _digest(path))
        for path in jeju_sources
    } == before


@pytest.mark.parametrize("other_region", ["gyeonggi", "seoul"])
def test_jeju_and_other_region_same_source_ids_do_not_collide(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    other_region: str,
) -> None:
    import_jeju_catalog(catalog_db, *jeju_sources, environment="test", apply=True)
    first_other = import_region_catalog(
        catalog_db,
        *jeju_sources,
        region=other_region,
        environment="test",
        apply=True,
    )
    second_other = import_region_catalog(
        catalog_db,
        *jeju_sources,
        region=other_region,
        environment="test",
        apply=True,
    )

    jeju_restaurant_id = restaurant_id_for_region("jeju", "101")
    other_restaurant_id = restaurant_id_for_region(other_region, "101")
    jeju_item_id = menu_item_id_for_region("jeju", "101", "11")
    other_item_id = menu_item_id_for_region(other_region, "101", "11")
    assert jeju_restaurant_id != other_restaurant_id
    assert jeju_item_id != other_item_id
    assert restaurant_slug_for_region("jeju", "101") == "jeju-kakao-101"
    assert restaurant_slug_for_region(other_region, "101") == f"{other_region}-kakao-101"
    assert restaurant_handle_for_region(other_region, "101") == f"@{other_region}.101"
    assert catalog_db.get(models.Restaurant, jeju_restaurant_id) is not None
    assert catalog_db.get(models.Restaurant, other_restaurant_id) is not None
    assert catalog_db.get(models.MenuItem, jeju_item_id) is not None
    other_item = catalog_db.get(models.MenuItem, other_item_id)
    assert other_item is not None
    assert other_item.name_en == "고기국수"
    assert other_item.price_amount == 9_000
    other_unknown_price = catalog_db.get(
        models.MenuItem,
        menu_item_id_for_region(other_region, "101", "12"),
    )
    assert other_unknown_price is not None
    assert other_unknown_price.price_amount is None
    assert (
        catalog_db.get(
            models.MenuItem,
            menu_item_id_for_region(other_region, "101", "91"),
        )
        is None
    )
    assert _count(catalog_db, models.Restaurant) == 6
    assert _count(catalog_db, models.MenuCategory) == 4
    assert _count(catalog_db, models.MenuItem) == 10
    assert first_other.resources["restaurants"].inserted == 3
    assert first_other.imported_unknown_price == 3
    assert first_other.normalized_invalid_price == 2
    assert second_other.resources["restaurants"].unchanged == 3
    assert second_other.resources["menu_items"].unchanged == 5


def test_seoul_reimport_preserves_owner_rating_and_reviews(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    import_region_catalog(
        catalog_db,
        *jeju_sources,
        region="seoul",
        environment="test",
        apply=True,
    )
    restaurant_id = restaurant_id_for_region("seoul", "101")
    item_id = menu_item_id_for_region("seoul", "101", "11")
    restaurant = catalog_db.get(models.Restaurant, restaurant_id)
    assert restaurant is not None

    user = models.User(
        id="seoul-reviewer",
        email="seoul-reviewer@example.test",
        password_hash=None,
        display_name="Seoul Reviewer",
        locale="ko",
        is_guest=False,
        is_active=True,
        roles=["owner"],
    )
    review = models.Review(
        id="seoul-review",
        menu_item_id=item_id,
        author_user_id=user.id,
        rating=5,
        body="그대로 보존",
        author_display_name=user.display_name,
        author_country_code="KR",
        tags=["preserved"],
        is_published=True,
    )
    catalog_db.add_all([user, review])
    restaurant.owner_user_id = user.id
    restaurant.rating_avg = Decimal("4.8")
    restaurant.rating_count = 17
    catalog_db.commit()

    summary = import_region_catalog(
        catalog_db,
        *jeju_sources,
        region="seoul",
        environment="test",
        apply=True,
    )
    catalog_db.expire_all()

    preserved = catalog_db.get(models.Restaurant, restaurant_id)
    assert preserved is not None
    assert preserved.owner_user_id == user.id
    assert preserved.rating_avg == Decimal("4.8")
    assert preserved.rating_count == 17
    assert catalog_db.get(models.Review, review.id) is not None
    assert summary.resources["restaurants"].unchanged == 3


def test_region_import_rejects_orphan_crawl_job(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
) -> None:
    source = sqlite3.connect(jeju_sources[1])
    try:
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("INSERT INTO crawl_jobs (kakao_place_id) VALUES ('999')")
        source.commit()
        assert Path(f"{jeju_sources[1]}-wal").stat().st_size > 0

        with pytest.raises(JejuImportError, match="missing jobs: 0, orphan jobs: 1"):
            import_region_catalog(
                catalog_db,
                *jeju_sources,
                region="seoul",
                environment="test",
                apply=True,
            )
    finally:
        source.close()
    assert _count(catalog_db, models.Restaurant) == 0


@pytest.mark.parametrize("region", ["gyeonggi", "seoul"])
def test_region_import_rejects_missing_crawl_job_for_in_region_place(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    region: str,
) -> None:
    with sqlite3.connect(jeju_sources[1]) as source:
        source.execute("DELETE FROM crawl_jobs WHERE kakao_place_id = '303'")

    with pytest.raises(JejuImportError, match="missing jobs: 1, orphan jobs: 0"):
        import_region_catalog(
            catalog_db,
            *jeju_sources,
            region=region,
            environment="test",
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0


@pytest.mark.parametrize("region", ["gyeonggi", "seoul"])
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_regional_import_refuses_deployment_environments(
    catalog_db: Session,
    jeju_sources: tuple[Path, Path],
    region: str,
    environment: str,
) -> None:
    with pytest.raises(JejuImportError, match="restricted to local and test"):
        import_region_catalog(
            catalog_db,
            *jeju_sources,
            region=region,
            environment=environment,
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0


def test_gyeonggi_cli_uses_default_paths_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_import(
        db: object,
        places_db: Path,
        menus_db: Path,
        **options: object,
    ) -> region_import_module.RegionImportSummary:
        captured.update(
            db=db,
            places_db=places_db,
            menus_db=menus_db,
            **options,
        )
        return region_import_module.RegionImportSummary(region=str(options["region"]))

    target_db = object()
    monkeypatch.setattr(
        region_import_module,
        "get_settings",
        lambda: SimpleNamespace(
            environment="test",
            database_url="sqlite:///target.sqlite3",
        ),
    )
    monkeypatch.setattr(
        region_import_module,
        "SessionLocal",
        lambda: nullcontext(target_db),
    )
    monkeypatch.setattr(region_import_module, "import_region_catalog", fake_import)

    assert region_import_module.region_main(["gyeonggi"]) == 0
    assert captured == {
        "db": target_db,
        "places_db": Path("databases/gyeonggi_full.sqlite3"),
        "menus_db": Path("databases/menus_gyeonggi.sqlite3"),
        "region": "gyeonggi",
        "environment": "test",
        "apply": False,
        "batch_size": region_import_module.DEFAULT_BATCH_SIZE,
    }
    assert "DRY RUN (rolled back): gyeonggi" in capsys.readouterr().out
