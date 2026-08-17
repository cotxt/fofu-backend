"""bind every authentication session to its issue-time guest state

Revision ID: b61f5e9a2c7d
Revises: f8b7c6d5e4a3
Create Date: 2026-08-12 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b61f5e9a2c7d"
down_revision: str | Sequence[str] | None = "f8b7c6d5e4a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAME = "auth_sessions"
_COLUMN_NAME = "is_guest_at_issue"


def _set_not_null() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE_NAME, schema=None) as batch_op:
            batch_op.alter_column(
                _COLUMN_NAME,
                existing_type=sa.Boolean(),
                nullable=False,
            )
        return
    op.alter_column(
        _TABLE_NAME,
        _COLUMN_NAME,
        existing_type=sa.Boolean(),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column(
        _TABLE_NAME,
        sa.Column(_COLUMN_NAME, sa.Boolean(), nullable=True),
    )
    # Scope is the only trustworthy legacy signal. Treat every historical guest
    # or QR session as guest-issued, even though this intentionally invalidates
    # registered users' old QR sessions after deployment.
    op.execute(
        sa.text(
            "UPDATE auth_sessions "
            "SET is_guest_at_issue = CASE "
            "WHEN scope IN ('guest', 'qr_guest') THEN TRUE ELSE FALSE END"
        )
    )
    _set_not_null()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE_NAME, schema=None) as batch_op:
            batch_op.drop_column(_COLUMN_NAME)
        return
    op.drop_column(_TABLE_NAME, _COLUMN_NAME)
