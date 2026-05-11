"""Drop the unused credit ledger balance snapshot column.

Revision ID: drop_credit_balance_after
Revises: ensure_credit_balance_after
Create Date: 2026-05-11 16:55:00.000000
"""

from alembic import op

revision = "drop_credit_balance_after"
down_revision = "ensure_credit_balance_after"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE credit_transaction DROP COLUMN IF EXISTS balance_after")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE credit_transaction ADD COLUMN IF NOT EXISTS balance_after NUMERIC",
    )
