"""Ensure credit ledger rows expose the ``balance_after`` snapshot column.

Revision ID: ensure_credit_balance_after
Revises: seed_personal_cm
Create Date: 2026-05-09 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "ensure_credit_balance_after"
down_revision = "seed_personal_cm"
branch_labels = None
depends_on = None


def _table_has_column(table_name: str, column_name: str) -> bool:
    """Return whether ``table_name`` already includes ``column_name``."""
    inspector = sa.inspect(op.get_bind())
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if _table_has_column("credit_transaction", "balance_after"):
        return
    op.add_column(
        "credit_transaction",
        sa.Column("balance_after", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    if not _table_has_column("credit_transaction", "balance_after"):
        return
    op.drop_column("credit_transaction", "balance_after")
