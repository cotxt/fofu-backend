"""add restaurant nearby lookup index

Revision ID: f2a6d9c8e7b1
Revises: d8e4f1a2b3c4
Create Date: 2026-08-16 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2a6d9c8e7b1"
down_revision: str | Sequence[str] | None = "d8e4f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_restaurants_published_latitude_longitude"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "restaurants",
        ["is_published", "latitude", "longitude"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="restaurants")
