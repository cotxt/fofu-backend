"""allow unknown prices in carts and prepared order cards

Revision ID: b5d1a0c9e7f2
Revises: a7c4e9d2b6f0
Create Date: 2026-08-17 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "b5d1a0c9e7f2"
down_revision: str | Sequence[str] | None = "a7c4e9d2b6f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NULLABLE_PRICE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cart_items", "unit_price_snapshot"),
    ("order_items", "unit_price_amount"),
    ("orders", "subtotal_amount"),
    ("orders", "total_amount"),
)


def _alter_prices(*, nullable: bool) -> None:
    for table_name, column_name in _NULLABLE_PRICE_COLUMNS:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=sa.Integer(),
                nullable=nullable,
            )


def upgrade() -> None:
    _alter_prices(nullable=True)


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        for table_name, column_name in _NULLABLE_PRICE_COLUMNS:
            null_count = int(
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(sa.table(table_name))
                    .where(sa.column(column_name).is_(None))
                )
                or 0
            )
            if null_count:
                raise RuntimeError(
                    f"Cannot restore {table_name}.{column_name} NOT NULL while "
                    f"{null_count} row(s) have an unknown price."
                )
    _alter_prices(nullable=False)
