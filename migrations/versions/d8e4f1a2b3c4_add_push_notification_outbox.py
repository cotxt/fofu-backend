"""add APNs device bindings and durable push outbox

Revision ID: d8e4f1a2b3c4
Revises: b61f5e9a2c7d
Create Date: 2026-08-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine.reflection import Inspector

revision: str = "d8e4f1a2b3c4"
down_revision: str | Sequence[str] | None = "b61f5e9a2c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEVICE_TABLE = "push_devices"
_DELIVERY_TABLE = "push_deliveries"


def _create_tables() -> None:
    op.create_table(
        _DEVICE_TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("auth_session_id", sa.String(length=36), nullable=False),
        sa.Column("installation_id", sa.String(length=128), nullable=False),
        sa.Column("device_token", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("locale", sa.String(length=35), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("invalidated_reason", sa.String(length=100), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform",
            "topic",
            "environment",
            "installation_id",
            name="uq_push_device_installation",
        ),
        sa.UniqueConstraint(
            "platform",
            "topic",
            "environment",
            "device_token",
            name="uq_push_device_token",
        ),
    )
    op.create_index("ix_push_devices_auth_session_id", _DEVICE_TABLE, ["auth_session_id"])
    op.create_index("ix_push_devices_user_active", _DEVICE_TABLE, ["user_id", "is_active"])
    op.create_index("ix_push_devices_user_id", _DEVICE_TABLE, ["user_id"])

    op.create_table(
        _DELIVERY_TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=36), nullable=False),
        sa.Column("notification_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apns_id", sa.String(length=36), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="ck_push_delivery_attempts"),
        sa.ForeignKeyConstraint(["device_id"], ["push_devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", "device_id", name="uq_push_delivery_event_device"),
    )
    op.create_index("ix_push_deliveries_device_id", _DELIVERY_TABLE, ["device_id"])
    op.create_index(
        "ix_push_deliveries_dispatch",
        _DELIVERY_TABLE,
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_push_deliveries_recipient_user_id",
        _DELIVERY_TABLE,
        ["recipient_user_id"],
    )
    op.create_index("ix_push_deliveries_status", _DELIVERY_TABLE, ["status"])


def _columns_match(
    inspector: Inspector,
    table: str,
    expected: dict[str, tuple[type[sa.types.TypeEngine], int | None, bool]],
) -> list[str]:
    issues: list[str] = []
    columns = {column["name"]: column for column in inspector.get_columns(table)}
    missing = sorted(set(expected) - columns.keys())
    unexpected = sorted(columns.keys() - set(expected))
    if missing:
        issues.append(f"{table} missing columns: {', '.join(missing)}")
    if unexpected:
        issues.append(f"{table} unexpected columns: {', '.join(unexpected)}")
    for name, (expected_type, expected_length, nullable) in expected.items():
        column = columns.get(name)
        if column is None:
            continue
        actual_type = column["type"]
        if not isinstance(actual_type, expected_type):
            issues.append(f"{table}.{name} has incompatible type {actual_type}")
        elif (
            expected_length is not None
            and getattr(actual_type, "length", None) != expected_length
        ):
            issues.append(f"{table}.{name} has incompatible length")
        if bool(column["nullable"]) != nullable:
            issues.append(f"{table}.{name} has incompatible nullability")
    if tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ()) != ("id",):
        issues.append(f"{table} has an incompatible primary key")
    return issues


def _existing_table_issues(inspector: Inspector) -> list[str]:
    device_columns: dict[str, tuple[type[sa.types.TypeEngine], int | None, bool]] = {
        "id": (sa.String, 36, False),
        "user_id": (sa.String, 36, False),
        "auth_session_id": (sa.String, 36, False),
        "installation_id": (sa.String, 128, False),
        "device_token": (sa.String, 512, False),
        "platform": (sa.String, 20, False),
        "topic": (sa.String, 255, False),
        "environment": (sa.String, 20, False),
        "locale": (sa.String, 35, True),
        "is_active": (sa.Boolean, None, False),
        "invalidated_reason": (sa.String, 100, True),
        "invalidated_at": (sa.DateTime, None, True),
        "last_registered_at": (sa.DateTime, None, False),
        "created_at": (sa.DateTime, None, False),
        "updated_at": (sa.DateTime, None, False),
    }
    delivery_columns: dict[str, tuple[type[sa.types.TypeEngine], int | None, bool]] = {
        "id": (sa.String, 36, False),
        "event_key": (sa.String, 160, False),
        "device_id": (sa.String, 36, False),
        "recipient_user_id": (sa.String, 36, False),
        "notification_type": (sa.String, 40, False),
        "title": (sa.String, 200, False),
        "body": (sa.String, 500, False),
        "payload": (sa.JSON, None, False),
        "status": (sa.String, 20, False),
        "attempt_count": (sa.Integer, None, False),
        "available_at": (sa.DateTime, None, False),
        "lease_id": (sa.String, 36, True),
        "lease_expires_at": (sa.DateTime, None, True),
        "apns_id": (sa.String, 36, True),
        "last_error_code": (sa.String, 100, True),
        "sent_at": (sa.DateTime, None, True),
        "created_at": (sa.DateTime, None, False),
        "updated_at": (sa.DateTime, None, False),
    }
    issues = _columns_match(inspector, _DEVICE_TABLE, device_columns)
    issues.extend(_columns_match(inspector, _DELIVERY_TABLE, delivery_columns))

    expected_uniques = {
        _DEVICE_TABLE: {
            "uq_push_device_installation": (
                "platform",
                "topic",
                "environment",
                "installation_id",
            ),
            "uq_push_device_token": (
                "platform",
                "topic",
                "environment",
                "device_token",
            ),
        },
        _DELIVERY_TABLE: {
            "uq_push_delivery_event_device": ("event_key", "device_id"),
        },
    }
    expected_indexes = {
        _DEVICE_TABLE: {
            "ix_push_devices_auth_session_id": ("auth_session_id",),
            "ix_push_devices_user_active": ("user_id", "is_active"),
            "ix_push_devices_user_id": ("user_id",),
        },
        _DELIVERY_TABLE: {
            "ix_push_deliveries_device_id": ("device_id",),
            "ix_push_deliveries_dispatch": (
                "status",
                "available_at",
                "lease_expires_at",
            ),
            "ix_push_deliveries_recipient_user_id": ("recipient_user_id",),
            "ix_push_deliveries_status": ("status",),
        },
    }
    for table in (_DEVICE_TABLE, _DELIVERY_TABLE):
        uniques = {
            row.get("name"): tuple(row.get("column_names") or ())
            for row in inspector.get_unique_constraints(table)
        }
        for name, columns in expected_uniques[table].items():
            if uniques.get(name) != columns:
                issues.append(f"{table} unique constraint {name} is missing or incompatible")
        indexes = {
            row.get("name"): tuple(row.get("column_names") or ())
            for row in inspector.get_indexes(table)
        }
        for name, columns in expected_indexes[table].items():
            if indexes.get(name) != columns:
                issues.append(f"{table} index {name} is missing or incompatible")
    return issues


def upgrade() -> None:
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        existing = {
            table for table in (_DEVICE_TABLE, _DELIVERY_TABLE) if inspector.has_table(table)
        }
        if existing:
            if existing != {_DEVICE_TABLE, _DELIVERY_TABLE}:
                raise RuntimeError(
                    "Existing push notification schema is incomplete; refusing to adopt it."
                )
            issues = _existing_table_issues(inspector)
            if issues:
                raise RuntimeError(
                    "Existing push notification schema is incompatible with migration "
                    f"{revision}: {'; '.join(issues)}. Refusing to adopt it."
                )
            return
    _create_tables()


def downgrade() -> None:
    op.drop_index("ix_push_deliveries_status", table_name=_DELIVERY_TABLE)
    op.drop_index("ix_push_deliveries_recipient_user_id", table_name=_DELIVERY_TABLE)
    op.drop_index("ix_push_deliveries_dispatch", table_name=_DELIVERY_TABLE)
    op.drop_index("ix_push_deliveries_device_id", table_name=_DELIVERY_TABLE)
    op.drop_table(_DELIVERY_TABLE)
    op.drop_index("ix_push_devices_user_id", table_name=_DEVICE_TABLE)
    op.drop_index("ix_push_devices_user_active", table_name=_DEVICE_TABLE)
    op.drop_index("ix_push_devices_auth_session_id", table_name=_DEVICE_TABLE)
    op.drop_table(_DEVICE_TABLE)
