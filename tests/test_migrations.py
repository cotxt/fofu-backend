from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "a29e9fc6a65d"
PREVIOUS_HEAD_REVISION = "d8e4f1a2b3c4"
HEAD_REVISION = "b5d1a0c9e7f2"
NEARBY_INDEX_NAME = "ix_restaurants_published_latitude_longitude"


def _test_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "FOFU_ENVIRONMENT": "test",
            "FOFU_DATABASE_URL": database_url,
            "FOFU_JWT_SECRET": "migration-test-secret-with-more-than-thirty-two-bytes",
            "FOFU_AUTO_CREATE_SCHEMA": "false",
            "FOFU_SEED_DEMO_DATA": "false",
        }
    )
    return environment


def _alembic_result(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_ROOT,
        env=_test_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )


def _run_alembic_url(database_url: str, *arguments: str) -> str:
    result = _alembic_result(database_url, *arguments)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return f"{result.stdout}\n{result.stderr}"


def _run_alembic(database_path: Path, *arguments: str) -> str:
    return _run_alembic_url(f"sqlite:///{database_path}", *arguments)


def _run_create_schema(database_path: Path) -> None:
    database_url = f"sqlite:///{database_path}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.database import create_schema; create_schema()",
        ],
        cwd=BACKEND_ROOT,
        env=_test_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_baseline_database_upgrades_to_head_without_model_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "baseline-upgrade.db"
    _run_alembic(database_path, "upgrade", BASELINE_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO orders (
                id, user_id, restaurant_id, status, serving_mode,
                subtotal_amount, total_amount, currency, korean_phrase,
                translated_phrase, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-order",
                "legacy-user",
                "legacy-restaurant",
                "prepared",
                "dine_in",
                1000,
                1000,
                "KRW",
                "테스트 주세요",
                "Test, please",
                "legacy-key",
                "2026-08-09 12:00:00",
                "2026-08-09 12:00:00",
            ),
        )
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        migrated = connection.execute(
            "SELECT request_fingerprint, response_snapshot FROM orders WHERE id = ?",
            ("legacy-order",),
        ).fetchone()
    assert migrated is not None
    assert migrated[0] == "legacy"
    assert json.loads(migrated[1]) == {}
    output = _run_alembic(database_path, "check")
    assert "No new upgrade operations detected" in output


def test_fresh_database_upgrades_to_head_without_model_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-head.db"
    _run_alembic(database_path, "upgrade", "head")
    output = _run_alembic(database_path, "check")
    assert "No new upgrade operations detected" in output


def test_unknown_menu_price_migration_makes_price_nullable(tmp_path: Path) -> None:
    database_path = tmp_path / "unknown-menu-price.db"
    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1]): {"not_null": bool(row[3])}
            for row in connection.execute("PRAGMA table_info('menu_items')")
        }

    assert columns["price_amount"]["not_null"] is False


def test_unknown_order_price_migration_makes_price_totals_nullable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unknown-order-price.db"
    _run_alembic(database_path, "upgrade", "head")

    expected_nullable_columns = {
        "cart_items": {"unit_price_snapshot"},
        "order_items": {"unit_price_amount"},
        "orders": {"subtotal_amount", "total_amount"},
    }
    with sqlite3.connect(database_path) as connection:
        for table_name, column_names in expected_nullable_columns.items():
            columns = {
                str(row[1]): {"not_null": bool(row[3])}
                for row in connection.execute(f"PRAGMA table_info('{table_name}')")
            }
            for column_name in column_names:
                assert columns[column_name]["not_null"] is False


def test_nearby_index_upgrade_guides_sqlite_plan_and_downgrades_safely(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nearby-index.db"
    _run_alembic(database_path, "upgrade", PREVIOUS_HEAD_REVISION)
    with sqlite3.connect(database_path) as connection:
        indexes_before = {
            str(row[1]) for row in connection.execute("PRAGMA index_list('restaurants')")
        }
    assert NEARBY_INDEX_NAME not in indexes_before

    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        indexes_after = {
            str(row[1]) for row in connection.execute("PRAGMA index_list('restaurants')")
        }
        indexed_columns = [
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info('{NEARBY_INDEX_NAME}')")
        ]
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT id, latitude, longitude, rating_avg, slug
                  FROM restaurants
                 WHERE is_published = 1
                   AND latitude BETWEEN ? AND ?
                   AND longitude BETWEEN ? AND ?
                """,
                (37.20, 37.31, 126.95, 127.10),
            )
        )

    assert indexed_columns == ["is_published", "latitude", "longitude"]
    assert {
        "ix_restaurants_is_published",
        "ix_restaurants_latitude",
        "ix_restaurants_longitude",
        NEARBY_INDEX_NAME,
    }.issubset(indexes_after)
    assert f"USING INDEX {NEARBY_INDEX_NAME}" in plan
    assert "is_published=? AND latitude>? AND latitude<?" in plan

    _run_alembic(database_path, "downgrade", PREVIOUS_HEAD_REVISION)
    with sqlite3.connect(database_path) as connection:
        indexes_downgraded = {
            str(row[1]) for row in connection.execute("PRAGMA index_list('restaurants')")
        }
    assert NEARBY_INDEX_NAME not in indexes_downgraded
    assert {
        "ix_restaurants_is_published",
        "ix_restaurants_latitude",
        "ix_restaurants_longitude",
    }.issubset(indexes_downgraded)


def test_upgrade_adopts_compatible_table_created_by_local_startup(tmp_path: Path) -> None:
    database_path = tmp_path / "create-all-before-migration.db"
    _run_alembic(database_path, "upgrade", "e71c943a0fb2")
    _run_create_schema(database_path)
    timestamp = "2026-08-12 06:00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, display_name, locale, is_guest, is_active, roles, created_at, updated_at
            ) VALUES (?, ?, 'en', 0, 1, '["customer"]', ?, ?)
            """,
            ("adopted-user", "Adopted User", timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO auth_identities (
                id, user_id, provider, subject, email, created_at, updated_at
            ) VALUES (?, ?, 'google', ?, ?, ?, ?)
            """,
            (
                "adopted-identity",
                "adopted-user",
                "adopted-google-subject",
                "adopted@example.com",
                timestamp,
                timestamp,
            ),
        )

    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        identity = connection.execute(
            "SELECT user_id, provider, subject FROM auth_identities WHERE id = ?",
            ("adopted-identity",),
        ).fetchone()
    assert revision == (HEAD_REVISION,)
    assert identity == ("adopted-user", "google", "adopted-google-subject")
    output = _run_alembic(database_path, "check")
    assert "No new upgrade operations detected" in output


def test_auth_session_guest_snapshot_backfills_by_legacy_scope(tmp_path: Path) -> None:
    database_path = tmp_path / "auth-session-guest-snapshot.db"
    _run_alembic(database_path, "upgrade", "f8b7c6d5e4a3")
    timestamp = "2026-08-12 08:00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, display_name, locale, is_guest, is_active, roles, created_at, updated_at
            ) VALUES (?, 'Legacy User', 'en', 0, 1, '["customer"]', ?, ?)
            """,
            ("legacy-session-user", timestamp, timestamp),
        )
        for index, scope in enumerate(("guest", "qr_guest", "full", "admin")):
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    id, user_id, refresh_token_hash, client_type, device_id, scope,
                    qr_restaurant_id, expires_at, created_at, last_used_at, revoked_at
                ) VALUES (?, ?, ?, 'ios', NULL, ?, NULL, ?, ?, ?, NULL)
                """,
                (
                    f"legacy-session-{index}",
                    "legacy-session-user",
                    f"{index:064d}",
                    scope,
                    "2027-08-12 08:00:00",
                    timestamp,
                    timestamp,
                ),
            )

    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        snapshots = connection.execute(
            """
            SELECT scope, is_guest_at_issue
              FROM auth_sessions
             ORDER BY id
            """
        ).fetchall()
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info('auth_sessions')").fetchall()
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert snapshots == [
        ("guest", 1),
        ("qr_guest", 1),
        ("full", 0),
        ("admin", 0),
    ]
    assert columns["is_guest_at_issue"][3] == 1
    assert revision == (HEAD_REVISION,)
    output = _run_alembic(database_path, "check")
    assert "No new upgrade operations detected" in output


def test_upgrade_rejects_incompatible_preexisting_auth_identity_table(tmp_path: Path) -> None:
    database_path = tmp_path / "incompatible-auth-identities.db"
    _run_alembic(database_path, "upgrade", "e71c943a0fb2")
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE auth_identities (id VARCHAR(36) PRIMARY KEY NOT NULL)")

    result = _alembic_result(f"sqlite:///{database_path}", "upgrade", "head")

    assert result.returncode != 0
    output = f"{result.stdout}\n{result.stderr}"
    assert "Existing auth_identities table is incompatible" in output
    assert "missing columns" in output
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("e71c943a0fb2",)


def test_owner_migration_reconciles_canonical_and_inferred_legacy_owners(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-owners.db"
    _run_alembic(database_path, "upgrade", "c4e8b7a21d36")
    timestamp = "2026-08-10 10:00:00"
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO users (
                id, email, display_name, locale, is_guest, is_active, roles,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'en', 0, 1, '["owner"]', ?, ?)
            """,
            [
                ("legacy-owner-a", "a@example.com", "Owner A", timestamp, timestamp),
                ("legacy-owner-b", "b@example.com", "Owner B", timestamp, timestamp),
                ("legacy-owner-c", "c@example.com", "Owner C", timestamp, timestamp),
            ],
        )
        connection.executemany(
            """
            INSERT INTO restaurants (
                id, slug, owner_user_id, name_en, description_en, handle, category,
                hero_style, address_en, latitude, longitude, currency, timezone_name,
                rating_avg, rating_count, is_verified, is_open, is_published,
                menu_revision, gallery, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '', ?, 'Test', 'charcoal', 'Test address',
                      37.5, 127.0, 'KRW', 'Asia/Seoul', 0.0, 0, 0, 1, 0, 1,
                      '[]', ?, ?)
            """,
            [
                (
                    "legacy-canonical",
                    "legacy-canonical",
                    "legacy-owner-a",
                    "Canonical Restaurant",
                    "legacy-canonical",
                    timestamp,
                    timestamp,
                ),
                (
                    "legacy-inferred",
                    "legacy-inferred",
                    None,
                    "Inferred Restaurant",
                    "legacy-inferred",
                    timestamp,
                    timestamp,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO restaurant_memberships
                (restaurant_id, user_id, role, status, created_at)
            VALUES (?, ?, 'owner', 'active', ?)
            """,
            [
                ("legacy-canonical", "legacy-owner-a", timestamp),
                ("legacy-canonical", "legacy-owner-b", timestamp),
                ("legacy-inferred", "legacy-owner-c", timestamp),
            ],
        )

    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        canonical_memberships = connection.execute(
            """
            SELECT user_id, status
              FROM restaurant_memberships
             WHERE restaurant_id = 'legacy-canonical' AND role = 'owner'
             ORDER BY user_id
            """
        ).fetchall()
        inferred_owner = connection.execute(
            "SELECT owner_user_id FROM restaurants WHERE id = 'legacy-inferred'"
        ).fetchone()

    assert canonical_memberships == [
        ("legacy-owner-a", "active"),
        ("legacy-owner-b", "revoked"),
    ]
    assert inferred_owner == ("legacy-owner-c",)


def test_postgresql_offline_upgrade_renders_all_revisions() -> None:
    output = _run_alembic_url(
        "postgresql+psycopg://fofu:secret@db/fofu",
        "upgrade",
        "head",
        "--sql",
    )
    assert "response_snapshot = '{}'" in output
    assert "ALTER COLUMN response_snapshot SET NOT NULL" in output
    assert "CREATE TABLE auth_identities" in output
    assert "ADD COLUMN is_guest_at_issue BOOLEAN" in output
    assert "WHEN scope IN ('guest', 'qr_guest') THEN TRUE ELSE FALSE END" in output
    assert "ALTER COLUMN is_guest_at_issue SET NOT NULL" in output
    assert "CREATE TABLE push_devices" in output
    assert "CREATE TABLE push_deliveries" in output
    assert (
        "CREATE INDEX ix_restaurants_published_latitude_longitude "
        "ON restaurants (is_published, latitude, longitude)" in output
    )
