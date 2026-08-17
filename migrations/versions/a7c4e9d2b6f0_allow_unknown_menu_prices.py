"""allow menu items without a known price

Revision ID: a7c4e9d2b6f0
Revises: f2a6d9c8e7b1
Create Date: 2026-08-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "a7c4e9d2b6f0"
down_revision: str | Sequence[str] | None = "f2a6d9c8e7b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("menu_items", schema=None) as batch_op:
        batch_op.alter_column(
            "price_amount",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    if not context.is_offline_mode():
        null_price_count = int(
            op.get_bind().scalar(
                sa.select(sa.func.count()).select_from(sa.table("menu_items")).where(
                    sa.column("price_amount").is_(None)
                )
            )
            or 0
        )
        if null_price_count:
            raise RuntimeError(
                "Cannot restore menu_items.price_amount NOT NULL while "
                f"{null_price_count} menu item(s) have an unknown price."
            )
    with op.batch_alter_table("menu_items", schema=None) as batch_op:
        batch_op.alter_column(
            "price_amount",
            existing_type=sa.Integer(),
            nullable=False,
        )
