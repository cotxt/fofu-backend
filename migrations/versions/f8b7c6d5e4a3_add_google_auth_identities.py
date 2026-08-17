"""add durable external authentication identities

Revision ID: f8b7c6d5e4a3
Revises: e71c943a0fb2
Create Date: 2026-08-12 06:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine.reflection import Inspector

revision: str = "f8b7c6d5e4a3"
down_revision: str | Sequence[str] | None = "e71c943a0fb2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "auth_identities"
_EXPECTED_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine], int | None, bool]] = {
    "id": (sa.String, 36, False),
    "user_id": (sa.String, 36, False),
    "provider": (sa.String, 30, False),
    "subject": (sa.String, 255, False),
    "email": (sa.String, 320, True),
    "created_at": (sa.DateTime, None, False),
    "updated_at": (sa.DateTime, None, False),
}
_EXPECTED_UNIQUES = {
    "uq_auth_identities_provider_subject": ("provider", "subject"),
    "uq_auth_identities_user_provider": ("user_id", "provider"),
}


def _existing_table_issues(inspector: Inspector) -> list[str]:
    issues: list[str] = []
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE_NAME)}
    expected_names = set(_EXPECTED_COLUMNS)
    missing = sorted(expected_names - columns.keys())
    unexpected = sorted(columns.keys() - expected_names)
    if missing:
        issues.append(f"missing columns: {', '.join(missing)}")
    if unexpected:
        issues.append(f"unexpected columns: {', '.join(unexpected)}")

    for name, (expected_type, expected_length, expected_nullable) in _EXPECTED_COLUMNS.items():
        column = columns.get(name)
        if column is None:
            continue
        column_type = column["type"]
        if not isinstance(column_type, expected_type):
            issues.append(f"{name} has incompatible type {column_type}")
        elif (
            expected_length is not None
            and getattr(column_type, "length", None) != expected_length
        ):
            issues.append(f"{name} has incompatible length {getattr(column_type, 'length', None)}")
        if bool(column["nullable"]) != expected_nullable:
            issues.append(f"{name} has incompatible nullability")

    primary_key = tuple(inspector.get_pk_constraint(_TABLE_NAME).get("constrained_columns") or ())
    if primary_key != ("id",):
        issues.append(f"primary key is {primary_key!r}, expected ('id',)")

    unique_constraints = {
        constraint.get("name"): tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(_TABLE_NAME)
    }
    for name, expected_columns in _EXPECTED_UNIQUES.items():
        if unique_constraints.get(name) != expected_columns:
            issues.append(f"unique constraint {name} is missing or incompatible")

    foreign_keys = inspector.get_foreign_keys(_TABLE_NAME)
    compatible_foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if tuple(foreign_key.get("constrained_columns") or ()) == ("user_id",)
        and foreign_key.get("referred_table") == "users"
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and str((foreign_key.get("options") or {}).get("ondelete", "")).upper() == "CASCADE"
    ]
    if len(compatible_foreign_keys) != 1 or len(foreign_keys) != 1:
        issues.append("user_id foreign key to users.id with ON DELETE CASCADE is missing")

    indexes = {index.get("name"): index for index in inspector.get_indexes(_TABLE_NAME)}
    user_index = indexes.get("ix_auth_identities_user_id")
    if (
        user_index is None
        or tuple(user_index.get("column_names") or ()) != ("user_id",)
        or bool(user_index.get("unique"))
    ):
        issues.append("index ix_auth_identities_user_id is missing or incompatible")
    return issues


def _create_auth_identities_table() -> None:
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "subject",
            name="uq_auth_identities_provider_subject",
        ),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_auth_identities_user_provider",
        ),
    )
    with op.batch_alter_table(_TABLE_NAME, schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_auth_identities_user_id"),
            ["user_id"],
            unique=False,
        )


def upgrade() -> None:
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(_TABLE_NAME):
            issues = _existing_table_issues(inspector)
            if issues:
                details = "; ".join(issues)
                raise RuntimeError(
                    "Existing auth_identities table is incompatible with migration "
                    f"{revision}: {details}. Refusing to adopt it."
                )
            return
    _create_auth_identities_table()


def downgrade() -> None:
    with op.batch_alter_table("auth_identities", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_identities_user_id"))
    op.drop_table("auth_identities")
