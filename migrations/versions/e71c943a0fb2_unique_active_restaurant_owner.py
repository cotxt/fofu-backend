"""enforce one active owner membership per restaurant

Revision ID: e71c943a0fb2
Revises: c4e8b7a21d36
Create Date: 2026-08-10 10:05:00.000000
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import context, op

revision: str = "e71c943a0fb2"
down_revision: str | Sequence[str] | None = "c4e8b7a21d36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_OWNER_PREDICATE = sa.text("role = 'owner' AND status = 'active'")
_INDEX_NAME = "uq_restaurant_memberships_active_owner"


def _reconcile_legacy_owners() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT
                r.id AS restaurant_id,
                r.owner_user_id AS canonical_owner_id,
                rm.user_id AS membership_user_id
            FROM restaurants AS r
            LEFT JOIN restaurant_memberships AS rm
              ON rm.restaurant_id = r.id
             AND rm.role = 'owner'
             AND rm.status = 'active'
            ORDER BY r.id, rm.created_at, rm.user_id
            """
        )
    ).mappings()
    restaurants: dict[str, dict[str, object]] = {}
    for row in rows:
        entry = restaurants.setdefault(
            str(row["restaurant_id"]),
            {"canonical_owner_id": row["canonical_owner_id"], "active_owner_ids": []},
        )
        if row["membership_user_id"] is not None:
            active_owner_ids = entry["active_owner_ids"]
            assert isinstance(active_owner_ids, list)
            active_owner_ids.append(str(row["membership_user_id"]))

    ambiguous = sorted(
        restaurant_id
        for restaurant_id, entry in restaurants.items()
        if entry["canonical_owner_id"] is None and len(entry["active_owner_ids"]) > 1
    )
    if ambiguous:
        identifiers = ", ".join(ambiguous[:20])
        suffix = " ..." if len(ambiguous) > 20 else ""
        raise RuntimeError(
            "Cannot infer a canonical owner for restaurants with multiple active owner "
            f"memberships: {identifiers}{suffix}. Set restaurants.owner_user_id before retrying."
        )

    now = datetime.now(timezone.utc)
    for restaurant_id, entry in restaurants.items():
        active_owner_ids = entry["active_owner_ids"]
        assert isinstance(active_owner_ids, list)
        canonical_owner_id = entry["canonical_owner_id"]
        if canonical_owner_id is None:
            if len(active_owner_ids) == 1:
                connection.execute(
                    sa.text(
                        "UPDATE restaurants SET owner_user_id = :owner_user_id "
                        "WHERE id = :restaurant_id"
                    ),
                    {
                        "owner_user_id": active_owner_ids[0],
                        "restaurant_id": restaurant_id,
                    },
                )
            continue

        canonical_owner_id = str(canonical_owner_id)
        connection.execute(
            sa.text(
                """
                UPDATE restaurant_memberships
                   SET status = 'revoked'
                 WHERE restaurant_id = :restaurant_id
                   AND role = 'owner'
                   AND status = 'active'
                   AND user_id != :owner_user_id
                """
            ),
            {"restaurant_id": restaurant_id, "owner_user_id": canonical_owner_id},
        )
        membership = connection.execute(
            sa.text(
                """
                SELECT 1
                  FROM restaurant_memberships
                 WHERE restaurant_id = :restaurant_id
                   AND user_id = :owner_user_id
                """
            ),
            {"restaurant_id": restaurant_id, "owner_user_id": canonical_owner_id},
        ).first()
        if membership is None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO restaurant_memberships
                        (restaurant_id, user_id, role, status, created_at)
                    VALUES
                        (:restaurant_id, :owner_user_id, 'owner', 'active', :created_at)
                    """
                ),
                {
                    "restaurant_id": restaurant_id,
                    "owner_user_id": canonical_owner_id,
                    "created_at": now,
                },
            )
        else:
            connection.execute(
                sa.text(
                    """
                    UPDATE restaurant_memberships
                       SET role = 'owner', status = 'active'
                     WHERE restaurant_id = :restaurant_id
                       AND user_id = :owner_user_id
                    """
                ),
                {"restaurant_id": restaurant_id, "owner_user_id": canonical_owner_id},
            )


def upgrade() -> None:
    if not context.is_offline_mode():
        _reconcile_legacy_owners()
    op.create_index(
        _INDEX_NAME,
        "restaurant_memberships",
        ["restaurant_id"],
        unique=True,
        sqlite_where=_ACTIVE_OWNER_PREDICATE,
        postgresql_where=_ACTIVE_OWNER_PREDICATE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="restaurant_memberships")
