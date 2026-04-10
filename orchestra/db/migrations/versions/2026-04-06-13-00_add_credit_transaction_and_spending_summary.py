"""Add credit_transaction table.

Revision ID: add_credit_ledger
Revises: add_assistant_cleanup_tasks
Create Date: 2026-04-06 13:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_credit_ledger"
down_revision = "add_whatsapp_call_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_transaction",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("billing_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("balance_after", sa.Numeric(), nullable=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("detail", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_credit_txn_ba_at",
        "credit_transaction",
        ["billing_account_id", "at"],
    )
    op.create_index(
        "ix_credit_txn_ba_category_at",
        "credit_transaction",
        ["billing_account_id", "category", "at"],
    )
    op.create_index(
        "ix_credit_txn_assistant_category_at",
        "credit_transaction",
        ["assistant_id", "category", "at"],
    )
    op.create_index(
        "ix_credit_txn_user_at",
        "credit_transaction",
        ["user_id", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_credit_txn_user_at", table_name="credit_transaction")
    op.drop_index(
        "ix_credit_txn_assistant_category_at",
        table_name="credit_transaction",
    )
    op.drop_index("ix_credit_txn_ba_category_at", table_name="credit_transaction")
    op.drop_index("ix_credit_txn_ba_at", table_name="credit_transaction")
    op.drop_table("credit_transaction")
