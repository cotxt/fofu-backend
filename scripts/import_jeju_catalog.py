from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import bindparam, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.database import SessionLocal

SAFE_REGION_IMPORT_ENVIRONMENTS = {"local", "test"}
SAFE_JEJU_IMPORT_ENVIRONMENTS = SAFE_REGION_IMPORT_ENVIRONMENTS
REGION_DATABASES = {
    "gyeonggi": (
        Path("databases/gyeonggi_full.sqlite3"),
        Path("databases/menus_gyeonggi.sqlite3"),
    ),
    "jeju": (
        Path("databases/jeju_full.sqlite3"),
        Path("databases/menus_jeju.sqlite3"),
    ),
    "seoul": (
        Path("databases/seoul_full.sqlite3"),
        Path("databases/menus_seoul.sqlite3"),
    ),
}
DEFAULT_PLACES_DB, DEFAULT_MENUS_DB = REGION_DATABASES["jeju"]
DEFAULT_BATCH_SIZE = 500
MAX_MENU_PRICE_AMOUNT = 500_000

_JEJU_IMPORT_NAMESPACE = uuid.UUID("84572379-743c-4f66-96d8-51bc3b232d50")
_MENU_CATEGORY_SLUG = "menu"
_SOURCE_PRIORITY = ("menus", "yogiyo_menus")

_PLACES_SCHEMA = {
    "places": {
        "kakao_place_id",
        "place_name",
        "category_name",
        "category_group_code",
        "phone",
        "address_name",
        "road_address_name",
        "longitude",
        "latitude",
    },
    "discoveries": {"kakao_place_id", "region", "in_region"},
}
_MENUS_SCHEMA = {
    "crawl_jobs": {"kakao_place_id"},
    "menu_groups": {"kakao_place_id", "source"},
    "menu_items": {
        "kakao_place_id",
        "source",
        "ordinal",
        "product_id",
        "name",
        "price",
        "description",
        "photo_url",
        "is_recommend",
        "sold_out",
    },
}

_CHOSEN_SOURCES_CTE = """
WITH chosen_sources AS (
    SELECT
        kakao_place_id,
        CASE
            WHEN MAX(CASE WHEN source = 'menus' THEN 1 ELSE 0 END) = 1
                THEN 'menus'
            ELSE 'yogiyo_menus'
        END AS source
    FROM menu_items
    WHERE source IN ('menus', 'yogiyo_menus')
    GROUP BY kakao_place_id
)
"""

_REGION_PLACES_CTE = """
WITH region_place_ids AS (
    SELECT DISTINCT kakao_place_id
    FROM discoveries
    WHERE region = ? AND in_region = 1
),
region_places AS (
    SELECT p.*
    FROM places AS p
    JOIN region_place_ids AS r ON r.kakao_place_id = p.kakao_place_id
)
"""


class RegionImportError(RuntimeError):
    """Raised when regional source databases cannot be imported safely."""


# Compatibility for callers of the original Jeju-only importer.
JejuImportError = RegionImportError


@dataclass
class ChangeCount:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass
class RegionImportSummary:
    region: str = "jeju"
    source_places: int = 0
    chosen_menu_places: int = 0
    selected_menu_rows: int = 0
    imported_menu_items: int = 0
    imported_unknown_price: int = 0
    normalized_invalid_price: int = 0
    # Kept for summary consumers; invalid prices are no longer skipped.
    skipped_invalid_price: int = 0
    skipped_missing_product_id: int = 0
    deduplicated_menu_items: int = 0
    resources: dict[str, ChangeCount] = field(default_factory=dict)

    def record(
        self,
        resource: str,
        outcome: Literal["inserted", "updated", "unchanged"],
    ) -> None:
        counter = self.resources.setdefault(resource, ChangeCount())
        setattr(counter, outcome, getattr(counter, outcome) + 1)

    def as_dict(self) -> dict[str, object]:
        return {
            "region": self.region,
            "source": {
                "places": self.source_places,
                "chosen_menu_places": self.chosen_menu_places,
                "selected_menu_rows": self.selected_menu_rows,
            },
            "menu_items": {
                "imported": self.imported_menu_items,
                "imported_unknown_price": self.imported_unknown_price,
                "normalized_invalid_price": self.normalized_invalid_price,
                "skipped_invalid_price": self.skipped_invalid_price,
                "skipped_missing_product_id": self.skipped_missing_product_id,
                "deduplicated": self.deduplicated_menu_items,
            },
            "resources": {
                name: asdict(counts) for name, counts in sorted(self.resources.items())
            },
        }


JejuImportSummary = RegionImportSummary


@dataclass
class _RegionSources:
    region: str
    places: sqlite3.Connection
    menus: sqlite3.Connection
    place_ids: set[str]
    chosen_sources: dict[str, str]
    cover_images: dict[str, str]


def normalize_region(region: str) -> str:
    normalized = region.strip().lower()
    if normalized not in REGION_DATABASES:
        supported = ", ".join(sorted(REGION_DATABASES))
        raise RegionImportError(
            f"Unsupported region {region!r}; expected one of: {supported}"
        )
    return normalized


def validate_region_environment(environment: str) -> None:
    if environment not in SAFE_REGION_IMPORT_ENVIRONMENTS:
        raise RegionImportError(
            "Regional catalog import is restricted to local and test environments; "
            "staging/production imports require a reviewed deployment process."
        )


def validate_jeju_environment(environment: str) -> None:
    validate_region_environment(environment)


def _stable_id(region: str, kind: str, *parts: str) -> str:
    normalized_region = normalize_region(region)
    # Keep IDs generated by the original Jeju-only importer unchanged. Other
    # regions include their key in the UUID name so equal Kakao IDs cannot alias.
    name_parts = (kind, *parts)
    if normalized_region != "jeju":
        name_parts = ("region", normalized_region, *name_parts)
    return str(uuid.uuid5(_JEJU_IMPORT_NAMESPACE, ":".join(name_parts)))


def restaurant_id_for_region(region: str, kakao_place_id: str) -> str:
    return _stable_id(region, "restaurant", str(kakao_place_id))


def restaurant_slug_for_region(region: str, kakao_place_id: str) -> str:
    return f"{normalize_region(region)}-kakao-{kakao_place_id}"


def restaurant_handle_for_region(region: str, kakao_place_id: str) -> str:
    return f"@{normalize_region(region)}.{kakao_place_id}"


def menu_category_id_for_region(region: str, kakao_place_id: str) -> str:
    return _stable_id(
        region,
        "menu-category",
        str(kakao_place_id),
        _MENU_CATEGORY_SLUG,
    )


def menu_item_id_for_region(
    region: str,
    kakao_place_id: str,
    product_id: str,
) -> str:
    return _stable_id(
        region,
        "menu-item",
        str(kakao_place_id),
        str(product_id),
    )


def restaurant_id_for(kakao_place_id: str) -> str:
    return restaurant_id_for_region("jeju", kakao_place_id)


def restaurant_slug_for(kakao_place_id: str) -> str:
    return restaurant_slug_for_region("jeju", kakao_place_id)


def restaurant_handle_for(kakao_place_id: str) -> str:
    return restaurant_handle_for_region("jeju", kakao_place_id)


def menu_category_id_for(kakao_place_id: str) -> str:
    return menu_category_id_for_region("jeju", kakao_place_id)


def menu_item_id_for(kakao_place_id: str, product_id: str) -> str:
    return menu_item_id_for_region("jeju", kakao_place_id, product_id)


def menu_item_slug_for(product_id: str) -> str:
    return f"kakao-{product_id}"


def normalize_https_image_url(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _numeric_identifier(value: object, label: str) -> str:
    identifier = str(value).strip()
    if not identifier or not identifier.isascii() or not identifier.isdigit():
        raise JejuImportError(f"{label} must be a non-empty numeric identifier: {value!r}")
    return identifier


def _has_nonempty_transaction_sidecar(path: Path) -> bool:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            if sidecar.stat().st_size > 0:
                return True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RegionImportError(f"Could not inspect SQLite sidecar: {sidecar}") from exc
    return False


@contextmanager
def _open_readonly_sqlite(path: Path, label: str) -> Iterator[sqlite3.Connection]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise JejuImportError(f"Could not open {label} database: {path}") from exc
    if not resolved.is_file():
        raise JejuImportError(f"{label} database is not a regular file: {path}")

    has_pending_transaction = _has_nonempty_transaction_sidecar(resolved)
    query = "mode=ro" if has_pending_transaction else "mode=ro&immutable=1"
    uri = f"{resolved.as_uri()}?{query}"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        if not has_pending_transaction and _has_nonempty_transaction_sidecar(resolved):
            raise RegionImportError(
                f"{label} database changed while its static snapshot was being opened"
            )
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        detail = ""
        if has_pending_transaction:
            detail = "; non-empty WAL/journal data was not ignored"
        raise JejuImportError(f"Could not read {label} database: {path}{detail}") from exc
    except Exception:
        if connection is not None:
            connection.close()
        raise
    try:
        yield connection
    finally:
        connection.close()


def _validate_sqlite_database(
    connection: sqlite3.Connection,
    required_schema: dict[str, set[str]],
    label: str,
) -> None:
    try:
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise JejuImportError(f"{label} database failed quick_check: {quick_check}")
        foreign_key_problem = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_problem is not None:
            raise JejuImportError(
                f"{label} database has a foreign-key violation: {tuple(foreign_key_problem)!r}"
            )
        for table_name, required_columns in required_schema.items():
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if table is None:
                raise JejuImportError(f"{label} database is missing table {table_name!r}")
            columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }
            missing = required_columns - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise JejuImportError(
                    f"{label} database table {table_name!r} is missing columns: {names}"
                )
    except sqlite3.Error as exc:
        raise JejuImportError(f"Could not validate {label} database") from exc


def _load_place_ids(connection: sqlite3.Connection, region: str) -> set[str]:
    place_ids: set[str] = set()
    rows = connection.execute(
        _REGION_PLACES_CTE + "SELECT kakao_place_id FROM region_places",
        (region,),
    )
    for row in rows:
        identifier = _numeric_identifier(row[0], "kakao_place_id")
        if identifier in place_ids:
            raise JejuImportError(f"places database contains duplicate ID {identifier}")
        place_ids.add(identifier)
    if not place_ids:
        raise JejuImportError("places database contains no places")
    return place_ids


def _validate_place_values(connection: sqlite3.Connection, region: str) -> None:
    invalid = connection.execute(
        _REGION_PLACES_CTE
        + """
        SELECT kakao_place_id
        FROM region_places
        WHERE place_name IS NULL
           OR trim(place_name) = ''
           OR length(place_name) > 160
           OR (trim(coalesce(road_address_name, '')) = ''
               AND trim(coalesce(address_name, '')) = '')
           OR length(
               CASE
                   WHEN trim(coalesce(road_address_name, '')) != ''
                       THEN road_address_name
                   ELSE address_name
               END
           ) > 300
           OR length(coalesce(phone, '')) > 30
           OR length(coalesce(category_name, '')) > 80
           OR latitude IS NULL
           OR longitude IS NULL
           OR latitude < -90
           OR latitude > 90
           OR longitude < -180
           OR longitude > 180
        LIMIT 1
        """,
        (region,),
    ).fetchone()
    if invalid is not None:
        raise JejuImportError(f"place {invalid[0]!r} cannot fit the Fofu restaurant schema")


def _load_chosen_sources(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        _CHOSEN_SOURCES_CTE
        + "SELECT kakao_place_id, source FROM chosen_sources ORDER BY kakao_place_id"
    )
    chosen: dict[str, str] = {}
    for row in rows:
        place_id = _numeric_identifier(row[0], "menu kakao_place_id")
        source = str(row[1])
        if source not in _SOURCE_PRIORITY:
            raise JejuImportError(f"Unsupported chosen menu source {source!r}")
        chosen[place_id] = source
    return chosen


def _validate_cross_database_keys(
    menus: sqlite3.Connection,
    place_ids: set[str],
    chosen_sources: dict[str, str],
) -> None:
    job_ids = {
        _numeric_identifier(row[0], "crawl job kakao_place_id")
        for row in menus.execute("SELECT kakao_place_id FROM crawl_jobs")
    }
    if job_ids != place_ids:
        missing_jobs = len(place_ids - job_ids)
        orphan_jobs = len(job_ids - place_ids)
        raise JejuImportError(
            "regional places/crawl_jobs kakao_place_id sets differ "
            f"(missing jobs: {missing_jobs}, orphan jobs: {orphan_jobs})"
        )
    unknown_chosen = set(chosen_sources) - place_ids
    if unknown_chosen:
        raise JejuImportError(
            f"chosen menu items reference {len(unknown_chosen)} places absent from places database"
        )
    orphan_item = menus.execute(
        """
        SELECT i.kakao_place_id, i.source, i.ordinal
        FROM menu_items AS i
        LEFT JOIN menu_groups AS g
          ON g.kakao_place_id = i.kakao_place_id AND g.source = i.source
        WHERE g.kakao_place_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan_item is not None:
        raise JejuImportError(
            "menu item has no matching menu group: "
            f"{tuple(orphan_item)!r}"
        )


def _validate_selected_menu_values(connection: sqlite3.Connection) -> None:
    invalid = connection.execute(
        _CHOSEN_SOURCES_CTE
        + """
        SELECT i.kakao_place_id, i.source, i.ordinal
        FROM menu_items AS i
        JOIN chosen_sources AS c
          ON c.kakao_place_id = i.kakao_place_id AND c.source = i.source
        WHERE i.name IS NULL
           OR trim(i.name) = ''
           OR length(i.name) > 160
           OR length(coalesce(i.description, '')) > 10000
           OR length(coalesce(i.photo_url, '')) > 2000
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise JejuImportError(
            "selected menu item cannot fit the Fofu menu schema: "
            f"{tuple(invalid)!r}"
        )


def _load_cover_images(
    connection: sqlite3.Connection,
    chosen_sources: dict[str, str],
) -> dict[str, str]:
    del chosen_sources  # Selection is performed by the CTE; retained for call-site clarity.
    rows = connection.execute(
        _CHOSEN_SOURCES_CTE
        + """
        SELECT i.kakao_place_id, i.photo_url
        FROM menu_items AS i
        JOIN chosen_sources AS c
          ON c.kakao_place_id = i.kakao_place_id
        WHERE i.source IN ('menus', 'yogiyo_menus')
          AND i.photo_url IS NOT NULL
          AND trim(i.photo_url) != ''
        ORDER BY
            i.kakao_place_id,
            CASE
                WHEN i.source = c.source THEN 0
                WHEN i.source = 'menus' THEN 1
                ELSE 2
            END,
            i.ordinal,
            i.product_id
        """
    )
    covers: dict[str, str] = {}
    for row in rows:
        place_id = str(row[0])
        if place_id in covers:
            continue
        normalized = normalize_https_image_url(row[1])
        if normalized is not None:
            covers[place_id] = normalized
    return covers


@contextmanager
def load_region_sources(
    region: str,
    places_db: Path,
    menus_db: Path,
) -> Iterator[_RegionSources]:
    """Open and fully validate both source databases without write access."""

    normalized_region = normalize_region(region)
    if places_db.expanduser().resolve() == menus_db.expanduser().resolve():
        raise JejuImportError("places and menus databases must be different files")
    with ExitStack() as stack:
        places = stack.enter_context(_open_readonly_sqlite(places_db, "places"))
        menus = stack.enter_context(_open_readonly_sqlite(menus_db, "menus"))
        _validate_sqlite_database(places, _PLACES_SCHEMA, "places")
        _validate_sqlite_database(menus, _MENUS_SCHEMA, "menus")
        place_ids = _load_place_ids(places, normalized_region)
        _validate_place_values(places, normalized_region)
        chosen_sources = _load_chosen_sources(menus)
        _validate_cross_database_keys(menus, place_ids, chosen_sources)
        _validate_selected_menu_values(menus)
        cover_images = _load_cover_images(menus, chosen_sources)
        yield _RegionSources(
            region=normalized_region,
            places=places,
            menus=menus,
            place_ids=place_ids,
            chosen_sources=chosen_sources,
            cover_images=cover_images,
        )


@contextmanager
def load_jeju_sources(
    places_db: Path,
    menus_db: Path,
) -> Iterator[_RegionSources]:
    with load_region_sources("jeju", places_db, menus_db) as sources:
        yield sources


def _chunks(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _cursor_batches(cursor: sqlite3.Cursor, size: int) -> Iterator[list[sqlite3.Row]]:
    while rows := cursor.fetchmany(size):
        yield rows


def _execute_updates(
    db: Session,
    table: Any,
    values: list[dict[str, Any]],
    fields: Sequence[str],
) -> None:
    if not values:
        return
    statement = (
        table.update()
        .where(table.c.id == bindparam("_target_id"))
        .values({name: bindparam(name) for name in fields})
    )
    db.execute(statement, values)


def _restaurant_category(row: sqlite3.Row) -> str:
    category = str(row["category_name"] or "").strip()
    if category:
        return category
    group_code = str(row["category_group_code"] or "").strip()
    return "카페" if group_code == "CE7" else "음식점"


_RESTAURANT_UPDATE_FIELDS = (
    "name_en",
    "name_ko",
    "handle",
    "category",
    "address_en",
    "address_ko",
    "phone",
    "latitude",
    "longitude",
    "currency",
    "timezone_name",
    "is_verified",
    "is_published",
    "cover_image_url",
)


def _restaurant_values(
    region: str,
    row: sqlite3.Row,
    cover_image_url: str | None,
) -> dict[str, Any]:
    kakao_place_id = _numeric_identifier(row["kakao_place_id"], "kakao_place_id")
    name = str(row["place_name"]).strip()
    road_address = str(row["road_address_name"] or "").strip()
    lot_address = str(row["address_name"] or "").strip()
    address = road_address or lot_address
    phone = str(row["phone"] or "").strip() or None
    return {
        "id": restaurant_id_for_region(region, kakao_place_id),
        "slug": restaurant_slug_for_region(region, kakao_place_id),
        "owner_user_id": None,
        "name_en": name,
        "name_ko": name,
        "description_en": "",
        "description_ko": None,
        "handle": restaurant_handle_for_region(region, kakao_place_id),
        "category": _restaurant_category(row),
        "hero_style": "charcoal",
        "address_en": address,
        "address_ko": address,
        "phone": phone,
        "latitude": float(row["latitude"]),
        "longitude": float(row["longitude"]),
        "currency": "KRW",
        "timezone_name": "Asia/Seoul",
        "rating_avg": Decimal("0.0"),
        "rating_count": 0,
        "is_verified": False,
        "is_open": True,
        "is_published": True,
        "menu_revision": 1,
        "cover_image_url": cover_image_url,
        "gallery": [],
    }


def _upsert_restaurant_batch(
    db: Session,
    region: str,
    rows: list[sqlite3.Row],
    covers: dict[str, str],
    summary: RegionImportSummary,
) -> set[str]:
    desired = [
        _restaurant_values(region, row, covers.get(str(row["kakao_place_id"])))
        for row in rows
    ]
    ids = [row["id"] for row in desired]
    slugs = [row["slug"] for row in desired]
    handles = [row["handle"] for row in desired]
    table = models.Restaurant.__table__
    select_fields = [
        table.c.id,
        table.c.slug,
        *[table.c[name] for name in _RESTAURANT_UPDATE_FIELDS],
    ]

    existing_by_id = {
        str(row["id"]): row
        for row in db.execute(select(*select_fields).where(table.c.id.in_(ids))).mappings()
    }
    slug_owners = {
        str(row["slug"]): str(row["id"])
        for row in db.execute(
            select(table.c.id, table.c.slug).where(table.c.slug.in_(slugs))
        ).mappings()
    }
    handle_owners = {
        str(row["handle"]): str(row["id"])
        for row in db.execute(
            select(table.c.id, table.c.handle).where(table.c.handle.in_(handles))
        ).mappings()
    }

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    inserted_ids: set[str] = set()
    for values in desired:
        target_id = str(values["id"])
        slug_owner = slug_owners.get(str(values["slug"]))
        handle_owner = handle_owners.get(str(values["handle"]))
        if slug_owner is not None and slug_owner != target_id:
            raise JejuImportError(
                f"restaurant slug {values['slug']!r} already belongs to {slug_owner!r}"
            )
        if handle_owner is not None and handle_owner != target_id:
            raise JejuImportError(
                f"restaurant handle {values['handle']!r} already belongs to {handle_owner!r}"
            )
        existing = existing_by_id.get(target_id)
        if existing is None:
            inserts.append(values)
            inserted_ids.add(target_id)
            summary.record("restaurants", "inserted")
            continue
        if str(existing["slug"]) != values["slug"]:
            raise JejuImportError(
                f"deterministic restaurant ID {target_id!r} belongs to another slug"
            )
        update_values = {
            name: (
                existing[name]
                if name == "cover_image_url" and values[name] is None
                else values[name]
            )
            for name in _RESTAURANT_UPDATE_FIELDS
        }
        if any(existing[name] != update_values[name] for name in _RESTAURANT_UPDATE_FIELDS):
            updates.append({"_target_id": target_id, **update_values})
            summary.record("restaurants", "updated")
        else:
            summary.record("restaurants", "unchanged")

    if inserts:
        db.execute(table.insert(), inserts)
    _execute_updates(db, table, updates, _RESTAURANT_UPDATE_FIELDS)
    return inserted_ids


_CATEGORY_UPDATE_FIELDS = ("name_en", "name_ko", "sort_order", "is_active")


def _upsert_category_batch(
    db: Session,
    region: str,
    place_ids: Sequence[str],
    summary: RegionImportSummary,
) -> set[str]:
    desired = [
        {
            "id": menu_category_id_for_region(region, place_id),
            "restaurant_id": restaurant_id_for_region(region, place_id),
            "slug": _MENU_CATEGORY_SLUG,
            "name_en": "Menu",
            "name_ko": "메뉴",
            "sort_order": 0,
            "is_active": True,
        }
        for place_id in place_ids
    ]
    ids = [row["id"] for row in desired]
    restaurant_ids = [row["restaurant_id"] for row in desired]
    table = models.MenuCategory.__table__
    select_fields = [
        table.c.id,
        table.c.restaurant_id,
        table.c.slug,
        *[table.c[name] for name in _CATEGORY_UPDATE_FIELDS],
    ]
    existing_by_id = {
        str(row["id"]): row
        for row in db.execute(select(*select_fields).where(table.c.id.in_(ids))).mappings()
    }
    pair_owners = {
        (str(row["restaurant_id"]), str(row["slug"])): str(row["id"])
        for row in db.execute(
            select(table.c.id, table.c.restaurant_id, table.c.slug).where(
                table.c.restaurant_id.in_(restaurant_ids),
                table.c.slug == _MENU_CATEGORY_SLUG,
            )
        ).mappings()
    }

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    changed_restaurants: set[str] = set()
    for values in desired:
        target_id = str(values["id"])
        restaurant_id = str(values["restaurant_id"])
        pair = (restaurant_id, _MENU_CATEGORY_SLUG)
        owner = pair_owners.get(pair)
        if owner is not None and owner != target_id:
            raise JejuImportError(
                f"menu category {pair!r} already belongs to another ID {owner!r}"
            )
        existing = existing_by_id.get(target_id)
        if existing is None:
            inserts.append(values)
            changed_restaurants.add(restaurant_id)
            summary.record("menu_categories", "inserted")
            continue
        if (str(existing["restaurant_id"]), str(existing["slug"])) != pair:
            raise JejuImportError(
                f"deterministic menu category ID {target_id!r} belongs to another category"
            )
        update_values = {name: values[name] for name in _CATEGORY_UPDATE_FIELDS}
        if any(existing[name] != update_values[name] for name in _CATEGORY_UPDATE_FIELDS):
            updates.append({"_target_id": target_id, **update_values})
            changed_restaurants.add(restaurant_id)
            summary.record("menu_categories", "updated")
        else:
            summary.record("menu_categories", "unchanged")

    if inserts:
        db.execute(table.insert(), inserts)
    _execute_updates(db, table, updates, _CATEGORY_UPDATE_FIELDS)
    return changed_restaurants


_MENU_ITEM_UPDATE_FIELDS = (
    "category_id",
    "name_en",
    "name_ko",
    "description_en",
    "description_ko",
    "price_amount",
    "currency",
    "badge",
    "image_url",
    "is_available",
    "sort_order",
)


def _normalized_menu_price(raw_price: Any) -> tuple[int | None, bool]:
    if (
        isinstance(raw_price, int)
        and not isinstance(raw_price, bool)
        and 1 <= raw_price <= MAX_MENU_PRICE_AMOUNT
    ):
        return raw_price, False
    return None, raw_price is not None


def _menu_item_values(
    region: str,
    row: sqlite3.Row,
    *,
    price_amount: int | None,
) -> dict[str, Any]:
    place_id = _numeric_identifier(row["kakao_place_id"], "menu kakao_place_id")
    product_id = _numeric_identifier(row["product_id"], "menu product_id")
    name = str(row["name"]).strip()
    description = str(row["description"] or "").strip()
    return {
        "id": menu_item_id_for_region(region, place_id, product_id),
        "restaurant_id": restaurant_id_for_region(region, place_id),
        "category_id": menu_category_id_for_region(region, place_id),
        "slug": menu_item_slug_for(product_id),
        "name_en": name,
        "name_ko": name,
        "pronunciation": None,
        "description_en": description,
        "description_ko": description or None,
        "price_amount": price_amount,
        "currency": "KRW",
        "serving_description": None,
        "spice_level": 0,
        "taste_profile": {},
        "local_tips": [],
        "badge": "Recommended" if bool(row["is_recommend"]) else None,
        "image_url": normalize_https_image_url(row["photo_url"]),
        "media": [],
        "is_available": not bool(row["sold_out"]),
        "sort_order": max(0, int(row["ordinal"])),
    }


def _upsert_menu_item_batch(
    db: Session,
    desired: list[dict[str, Any]],
    summary: RegionImportSummary,
) -> set[str]:
    ids = [row["id"] for row in desired]
    restaurant_ids = list({str(row["restaurant_id"]) for row in desired})
    slugs = list({str(row["slug"]) for row in desired})
    table = models.MenuItem.__table__
    select_fields = [
        table.c.id,
        table.c.restaurant_id,
        table.c.slug,
        *[table.c[name] for name in _MENU_ITEM_UPDATE_FIELDS],
    ]
    existing_by_id = {
        str(row["id"]): row
        for row in db.execute(select(*select_fields).where(table.c.id.in_(ids))).mappings()
    }
    pair_owners = {
        (str(row["restaurant_id"]), str(row["slug"])): str(row["id"])
        for row in db.execute(
            select(table.c.id, table.c.restaurant_id, table.c.slug).where(
                table.c.restaurant_id.in_(restaurant_ids),
                table.c.slug.in_(slugs),
            )
        ).mappings()
    }

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    changed_restaurants: set[str] = set()
    for values in desired:
        target_id = str(values["id"])
        restaurant_id = str(values["restaurant_id"])
        pair = (restaurant_id, str(values["slug"]))
        owner = pair_owners.get(pair)
        if owner is not None and owner != target_id:
            raise JejuImportError(f"menu item slug {pair!r} already belongs to {owner!r}")
        existing = existing_by_id.get(target_id)
        if existing is None:
            inserts.append(values)
            changed_restaurants.add(restaurant_id)
            summary.record("menu_items", "inserted")
            continue
        if (str(existing["restaurant_id"]), str(existing["slug"])) != pair:
            raise JejuImportError(
                f"deterministic menu item ID {target_id!r} belongs to another item"
            )
        update_values = {name: values[name] for name in _MENU_ITEM_UPDATE_FIELDS}
        if any(existing[name] != update_values[name] for name in _MENU_ITEM_UPDATE_FIELDS):
            updates.append({"_target_id": target_id, **update_values})
            changed_restaurants.add(restaurant_id)
            summary.record("menu_items", "updated")
        else:
            summary.record("menu_items", "unchanged")

    if inserts:
        db.execute(table.insert(), inserts)
    _execute_updates(db, table, updates, _MENU_ITEM_UPDATE_FIELDS)
    return changed_restaurants


def _import_restaurants(
    db: Session,
    sources: _RegionSources,
    summary: RegionImportSummary,
    batch_size: int,
) -> set[str]:
    cursor = sources.places.execute(
        _REGION_PLACES_CTE
        + """
        SELECT kakao_place_id, place_name, category_name, category_group_code,
               phone, address_name, road_address_name, longitude, latitude
        FROM region_places
        ORDER BY kakao_place_id
        """,
        (sources.region,),
    )
    new_restaurant_ids: set[str] = set()
    for rows in _cursor_batches(cursor, batch_size):
        new_restaurant_ids.update(
            _upsert_restaurant_batch(
                db,
                sources.region,
                rows,
                sources.cover_images,
                summary,
            )
        )
    return new_restaurant_ids


def _import_categories(
    db: Session,
    sources: _RegionSources,
    summary: RegionImportSummary,
    batch_size: int,
) -> set[str]:
    changed: set[str] = set()
    place_ids = sorted(sources.chosen_sources)
    for batch in _chunks(place_ids, batch_size):
        changed.update(_upsert_category_batch(db, sources.region, batch, summary))
    return changed


def _selected_menu_cursor(connection: sqlite3.Connection) -> sqlite3.Cursor:
    return connection.execute(
        _CHOSEN_SOURCES_CTE
        + """
        SELECT i.kakao_place_id, i.source, i.ordinal, i.product_id, i.name,
               i.price, i.description, i.photo_url, i.is_recommend, i.sold_out
        FROM menu_items AS i
        JOIN chosen_sources AS c
          ON c.kakao_place_id = i.kakao_place_id AND c.source = i.source
        ORDER BY i.kakao_place_id, i.ordinal, i.product_id
        """
    )


def _import_menu_items(
    db: Session,
    sources: _RegionSources,
    summary: RegionImportSummary,
    batch_size: int,
) -> set[str]:
    changed: set[str] = set()
    batch: list[dict[str, Any]] = []
    seen_products: set[tuple[str, str]] = set()
    for row in _selected_menu_cursor(sources.menus):
        summary.selected_menu_rows += 1
        if row["product_id"] is None:
            summary.skipped_missing_product_id += 1
            continue
        place_id = _numeric_identifier(row["kakao_place_id"], "menu kakao_place_id")
        product_id = _numeric_identifier(row["product_id"], "menu product_id")
        product_key = (place_id, product_id)
        if product_key in seen_products:
            summary.deduplicated_menu_items += 1
            continue
        seen_products.add(product_key)
        price_amount, normalized_invalid = _normalized_menu_price(row["price"])
        if price_amount is None:
            summary.imported_unknown_price += 1
        if normalized_invalid:
            summary.normalized_invalid_price += 1
        batch.append(
            _menu_item_values(
                sources.region,
                row,
                price_amount=price_amount,
            )
        )
        if len(batch) >= batch_size:
            changed.update(_upsert_menu_item_batch(db, batch, summary))
            summary.imported_menu_items += len(batch)
            batch = []
    if batch:
        changed.update(_upsert_menu_item_batch(db, batch, summary))
        summary.imported_menu_items += len(batch)
    return changed


def _increment_menu_revisions(
    db: Session,
    restaurant_ids: set[str],
    new_restaurant_ids: set[str],
    batch_size: int,
) -> None:
    existing_changed = sorted(restaurant_ids - new_restaurant_ids)
    table = models.Restaurant.__table__
    for batch in _chunks(existing_changed, batch_size):
        db.execute(
            table.update()
            .where(table.c.id.in_(batch))
            .values(menu_revision=table.c.menu_revision + 1)
        )


def import_region_catalog(
    db: Session,
    places_db: Path,
    menus_db: Path,
    *,
    region: str,
    environment: str,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> RegionImportSummary:
    """Validate and atomically upsert one regional catalog; roll back by default."""

    normalized_region = normalize_region(region)
    validate_region_environment(environment)
    if batch_size < 1:
        raise RegionImportError("batch_size must be at least 1")
    summary = RegionImportSummary(region=normalized_region)
    try:
        with load_region_sources(normalized_region, places_db, menus_db) as sources:
            summary.source_places = len(sources.place_ids)
            summary.chosen_menu_places = len(sources.chosen_sources)
            new_restaurant_ids = _import_restaurants(db, sources, summary, batch_size)
            menu_changed = _import_categories(db, sources, summary, batch_size)
            menu_changed.update(_import_menu_items(db, sources, summary, batch_size))
            _increment_menu_revisions(db, menu_changed, new_restaurant_ids, batch_size)
            db.flush()
        if apply:
            db.commit()
        else:
            db.rollback()
        return summary
    except Exception:
        db.rollback()
        raise


def import_jeju_catalog(
    db: Session,
    places_db: Path,
    menus_db: Path,
    *,
    environment: str,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> JejuImportSummary:
    """Compatibility wrapper for the original Jeju importer."""

    return import_region_catalog(
        db,
        places_db,
        menus_db,
        region="jeju",
        environment=environment,
        apply=apply,
        batch_size=batch_size,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and upsert the Jeju places/menu SQLite catalogs. "
            "The default is a transactionally checked dry-run."
        )
    )
    parser.add_argument(
        "places_db",
        type=Path,
        nargs="?",
        default=DEFAULT_PLACES_DB,
        help=f"Places SQLite database (default: {DEFAULT_PLACES_DB})",
    )
    parser.add_argument(
        "menus_db",
        type=Path,
        nargs="?",
        default=DEFAULT_MENUS_DB,
        help=f"Menus SQLite database (default: {DEFAULT_MENUS_DB})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the import (without this flag every change is rolled back)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Target write batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    return parser


def _region_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and upsert a regional places/menu SQLite catalog. "
            "The default is a transactionally checked dry-run."
        )
    )
    parser.add_argument("region", choices=sorted(REGION_DATABASES))
    parser.add_argument(
        "places_db",
        type=Path,
        nargs="?",
        help="Places SQLite database (defaults to the selected region catalog)",
    )
    parser.add_argument(
        "menus_db",
        type=Path,
        nargs="?",
        help="Menus SQLite database (defaults to the selected region catalog)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the import (without this flag every change is rolled back)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Target write batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    return parser


def region_main(argv: list[str] | None = None) -> int:
    args = _region_parser().parse_args(argv)
    region = normalize_region(args.region)
    default_places, default_menus = REGION_DATABASES[region]
    places_db = args.places_db or default_places
    menus_db = args.menus_db or default_menus
    settings = get_settings()
    try:
        validate_region_environment(settings.environment)
        with SessionLocal() as db:
            summary = import_region_catalog(
                db,
                places_db,
                menus_db,
                region=region,
                environment=settings.environment,
                apply=args.apply,
                batch_size=args.batch_size,
            )
    except RegionImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = "APPLIED" if args.apply else "DRY RUN (rolled back)"
    safe_database_url = make_url(settings.database_url).render_as_string(hide_password=True)
    print(f"{mode}: {region} {places_db} + {menus_db} -> {safe_database_url}")
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    try:
        validate_jeju_environment(settings.environment)
        with SessionLocal() as db:
            summary = import_jeju_catalog(
                db,
                args.places_db,
                args.menus_db,
                environment=settings.environment,
                apply=args.apply,
                batch_size=args.batch_size,
            )
    except JejuImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = "APPLIED" if args.apply else "DRY RUN (rolled back)"
    safe_database_url = make_url(settings.database_url).render_as_string(hide_password=True)
    print(f"{mode}: {args.places_db} + {args.menus_db} -> {safe_database_url}")
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
