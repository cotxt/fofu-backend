"""store exact prepared order-card idempotency snapshots

Revision ID: c4e8b7a21d36
Revises: a29e9fc6a65d
Create Date: 2026-08-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8b7a21d36"
down_revision: str | Sequence[str] | None = "a29e9fc6a65d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("response_snapshot", sa.JSON(), nullable=True))

    # Baseline orders predate the canonical request inputs and exact response snapshot.
    # A deliberately non-SHA fingerprint makes their old key fail closed with HTTP 409.
    # Static SQL also renders correctly for PostgreSQL's offline migration mode, where
    # SQLAlchemy deliberately has no generic JSON literal renderer.
    op.execute("UPDATE orders SET request_fingerprint = 'legacy', response_snapshot = '{}'")

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.alter_column(
            "request_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "response_snapshot",
            existing_type=sa.JSON(),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("response_snapshot")
        batch_op.drop_column("request_fingerprint")
