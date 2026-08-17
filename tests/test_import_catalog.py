from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import models
from app.database import Base
from scripts.import_catalog import (
    CatalogImportError,
    CatalogManifest,
    import_catalog,
    load_catalog_manifest,
)

EXAMPLE_MANIFEST = Path(__file__).parents[1] / "examples" / "catalog.fictional.json"


@pytest.fixture
def catalog_db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def _raw_manifest() -> dict[str, object]:
    return json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))


def _manifest(raw: dict[str, object] | None = None) -> CatalogManifest:
    if raw is None:
        return load_catalog_manifest(EXAMPLE_MANIFEST)
    return CatalogManifest.model_validate(raw)


def _count(db: Session, model: type[object]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_default_dry_run_validates_and_rolls_back_every_row(catalog_db: Session) -> None:
    summary = import_catalog(catalog_db, _manifest(), environment="test")

    assert summary.resources["restaurants"].inserted == 1
    assert summary.resources["menu_categories"].inserted == 1
    assert summary.resources["menu_items"].inserted == 1
    assert _count(catalog_db, models.Restaurant) == 0
    assert _count(catalog_db, models.MenuItem) == 0
    assert _count(catalog_db, models.Ingredient) == 0


def test_apply_is_idempotent_and_keeps_stable_ids(catalog_db: Session) -> None:
    manifest = _manifest()
    first = import_catalog(catalog_db, manifest, environment="test", apply=True)
    restaurant = catalog_db.scalar(select(models.Restaurant))
    item = catalog_db.scalar(select(models.MenuItem))
    assert restaurant is not None
    assert item is not None
    restaurant_id = restaurant.id
    item_id = item.id
    menu_revision = restaurant.menu_revision

    second = import_catalog(catalog_db, manifest, environment="test", apply=True)
    catalog_db.expire_all()

    assert first.resources["restaurants"].inserted == 1
    assert second.resources["restaurants"].unchanged == 1
    assert second.resources["menu_items"].unchanged == 1
    assert _count(catalog_db, models.Restaurant) == 1
    assert _count(catalog_db, models.MenuItem) == 1
    assert catalog_db.get(models.Restaurant, restaurant_id).menu_revision == menu_revision  # type: ignore[union-attr]
    assert catalog_db.get(models.MenuItem, item_id) is not None


def test_update_preserves_ids_user_links_and_omitted_rows(catalog_db: Session) -> None:
    initial = _raw_manifest()
    restaurants = initial["restaurants"]
    assert isinstance(restaurants, list)
    restaurant_spec = restaurants[0]
    assert isinstance(restaurant_spec, dict)
    categories = restaurant_spec["menu_categories"]
    assert isinstance(categories, list)
    second_category = copy.deepcopy(categories[0])
    second_category["slug"] = "omitted-later"
    second_category["name_en"] = "Omitted later but preserved"
    second_category["name_ko"] = "나중에 생략해도 보존"
    second_category["translations"] = []
    second_category["items"][0]["slug"] = "omitted-later-item"
    second_category["items"][0]["name_en"] = "Omitted Later Item"
    second_category["items"][0]["translations"] = []
    categories.append(second_category)
    import_catalog(catalog_db, _manifest(initial), environment="test", apply=True)

    restaurant = catalog_db.scalar(select(models.Restaurant))
    first_item = catalog_db.scalar(
        select(models.MenuItem).where(models.MenuItem.slug == "fictional-tofu-rice-bowl")
    )
    omitted_item = catalog_db.scalar(
        select(models.MenuItem).where(models.MenuItem.slug == "omitted-later-item")
    )
    assert restaurant is not None
    assert first_item is not None
    assert omitted_item is not None
    restaurant_id = restaurant.id
    item_id = first_item.id
    omitted_item_id = omitted_item.id

    user = models.User(
        id="catalog-import-preserved-user",
        email="catalog-owner@example.test",
        password_hash=None,
        display_name="Preserved Owner",
        locale="en",
        is_guest=False,
        is_active=True,
        roles=["owner"],
    )
    catalog_db.add(user)
    restaurant.owner_user_id = user.id
    catalog_db.add(
        models.SavedRestaurant(user_id=user.id, restaurant_id=restaurant.id)
    )
    catalog_db.commit()

    update = _raw_manifest()
    update_restaurants = update["restaurants"]
    assert isinstance(update_restaurants, list)
    update_restaurant = update_restaurants[0]
    assert isinstance(update_restaurant, dict)
    update_restaurant["name_en"] = "Updated Fictional Kitchen"
    update_categories = update_restaurant["menu_categories"]
    assert isinstance(update_categories, list)
    update_item = update_categories[0]["items"][0]
    update_item["price_amount"] = 13500
    update_item.pop("ingredients")

    import_catalog(catalog_db, _manifest(update), environment="test", apply=True)
    catalog_db.expire_all()

    updated_restaurant = catalog_db.get(models.Restaurant, restaurant_id)
    updated_item = catalog_db.get(models.MenuItem, item_id)
    assert updated_restaurant is not None
    assert updated_item is not None
    assert updated_restaurant.name_en == "Updated Fictional Kitchen"
    assert updated_restaurant.owner_user_id == user.id
    assert updated_item.price_amount == 13_500
    assert len(updated_item.ingredient_links) == 2
    assert catalog_db.get(models.MenuItem, omitted_item_id) is not None
    assert catalog_db.get(models.SavedRestaurant, (user.id, restaurant_id)) is not None


def test_invalid_import_is_atomic_and_structural_errors_precede_db_work(
    catalog_db: Session,
) -> None:
    raw = _raw_manifest()
    restaurants = raw["restaurants"]
    assert isinstance(restaurants, list)
    restaurant = restaurants[0]
    assert isinstance(restaurant, dict)
    categories = restaurant["menu_categories"]
    assert isinstance(categories, list)
    duplicate = copy.deepcopy(categories[0])
    duplicate["slug"] = "different-category"
    categories.append(duplicate)
    with pytest.raises(ValidationError, match="duplicate menu item slugs"):
        _manifest(raw)
    assert _count(catalog_db, models.Restaurant) == 0

    manifest = _manifest()
    import_catalog(catalog_db, manifest, environment="test", apply=True)
    existing_restaurant = catalog_db.scalar(select(models.Restaurant))
    existing_item = catalog_db.scalar(select(models.MenuItem))
    assert existing_restaurant is not None
    assert existing_item is not None
    old_name = existing_restaurant.name_en
    catalog_db.add(
        models.MenuItemAllergen(
            menu_item_id=existing_item.id,
            allergen_code="fictional-soy",
            relation_type="free_from",
            verification_status="unverified",
            source="deliberately ambiguous test row",
        )
    )
    catalog_db.commit()

    changed = _raw_manifest()
    changed_restaurants = changed["restaurants"]
    assert isinstance(changed_restaurants, list)
    changed_restaurants[0]["name_en"] = "Must Roll Back"
    with pytest.raises(CatalogImportError, match="ambiguous existing allergen"):
        import_catalog(
            catalog_db,
            _manifest(changed),
            environment="test",
            apply=True,
        )
    catalog_db.expire_all()
    assert catalog_db.get(models.Restaurant, existing_restaurant.id).name_en == old_name  # type: ignore[union-attr]


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_catalog_import_refuses_deployment_environments(
    catalog_db: Session, environment: str
) -> None:
    with pytest.raises(CatalogImportError, match="restricted to local and test"):
        import_catalog(
            catalog_db,
            _manifest(),
            environment=environment,
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0


def test_unknown_taxonomy_reference_leaves_database_empty(catalog_db: Session) -> None:
    raw = _raw_manifest()
    restaurants = raw["restaurants"]
    assert isinstance(restaurants, list)
    item = restaurants[0]["menu_categories"][0]["items"][0]
    item["ingredients"][0]["code"] = "not-declared-anywhere"

    with pytest.raises(CatalogImportError, match="unknown ingredient codes"):
        import_catalog(
            catalog_db,
            _manifest(raw),
            environment="test",
            apply=True,
        )
    assert _count(catalog_db, models.Restaurant) == 0
    assert _count(catalog_db, models.Ingredient) == 0
