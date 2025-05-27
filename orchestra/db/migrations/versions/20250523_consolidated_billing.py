"""Consolidated Billing Infrastructure

This migration adds the complete billing system including:

USERS table additions:
- credits: User's current credit balance
- autorecharge: Auto-recharge enabled flag
- autorecharge_threshold: Threshold for triggering auto-recharge
- autorecharge_qty: Quantity to recharge automatically
- store_prompts: Whether to store user prompts
- credit_balance: BigInteger credit balance field
- billing_state: User's billing status (OK, PAST_DUE, SUSPENDED)

RECHARGE table additions:
- amount_usd: USD amount for the recharge
- status: Recharge status enum (PENDING_INVOICE, INVOICE_CREATED, PAID, FAILED)
- stripe_invoice_id: Associated Stripe invoice ID
- invoice_group: Month-end date for grouping charges
- Index on (status, invoice_group) for efficient querying

This replaces the previous separate migrations:
- 20240401_add_billing_columns.py
- 20250520_monthly_invoicing.py

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# ──────────────────────────────────────────────────────────────────────────────
revision: str = "20250523_consolidated_billing"
down_revision: str | None = "df96cf4dc6f9"
branch_labels = None
depends_on = None
# ──────────────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    # 1 ───────────── USERS wallet / settings  ────────────────────────────────
    op.add_column(
        "users",
        sa.Column("credits", sa.Numeric(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column(
            "autorecharge",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "autorecharge_threshold",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "autorecharge_qty",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "store_prompts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # 2 ───────────── Extra tweaks to USERS  ──────────────────────────────────
    op.add_column("users", sa.Column("credit_balance", sa.BigInteger()))
    op.alter_column(
        "users",
        "billing_state",
        existing_type=sa.VARCHAR(),
        nullable=True,
        existing_server_default=sa.text("'OK'"),
    )

    # 3 ───────────── RECHARGE additions  ─────────────────────────────────────
    op.add_column(
        "recharge",
        sa.Column("amount_usd", sa.Numeric(), nullable=False),
    )

    recharge_status = sa.Enum(
        "PENDING_INVOICE",
        "INVOICE_CREATED",
        "PAID",
        "FAILED",
        name="recharge_status",
    )
    recharge_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "recharge",
        sa.Column(
            "status",
            recharge_status,
            nullable=False,
            server_default="PENDING_INVOICE",
        ),
    )
    op.add_column("recharge", sa.Column("stripe_invoice_id", sa.String()))
    op.add_column("recharge", sa.Column("invoice_group", sa.Date()))
    op.create_index(
        "idx_recharge_pending",
        "recharge",
        ["status", "invoice_group"],
    )

    op.alter_column(
        "recharge",
        "at",
        existing_type=sa.TIMESTAMP(),
        nullable=False,
        server_default=sa.func.now(),
    )

    # 4 ───────────── enum DDL on very old Postgres versions (optional) ───────
    op.execute("COMMIT")


def downgrade() -> None:
    # 3 ← undo RECHARGE changes ---------------------------------------------
    op.drop_index("idx_recharge_pending", table_name="recharge")
    op.drop_column("recharge", "invoice_group")
    op.drop_column("recharge", "stripe_invoice_id")
    op.drop_column("recharge", "status")
    op.drop_column("recharge", "amount_usd")
    sa.Enum(name="recharge_status").drop(op.get_bind(), checkfirst=True)

    # 2 ← undo USERS tweaks ---------------------------------------------------
    op.alter_column(
        "users",
        "billing_state",
        existing_type=sa.VARCHAR(),
        nullable=False,
        existing_server_default=sa.text("'OK'"),
    )
    op.drop_column("users", "credit_balance")

    # 1 ← drop USERS wallet columns ------------------------------------------
    op.drop_column("users", "store_prompts")
    op.drop_column("users", "autorecharge_qty")
    op.drop_column("users", "autorecharge_threshold")
    op.drop_column("users", "autorecharge")
    op.drop_column("users", "credits")
