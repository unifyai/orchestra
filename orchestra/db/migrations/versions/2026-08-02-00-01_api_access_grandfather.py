"""Grandfather accounts already using the programmatic API.

Gating the API on payment history would cut off never-paid accounts with
live integrations. This backfills a per-account exemption for anyone who
has already spent through the UniLLM proxy, so the gate applies only to
accounts that have not used it yet.

Identifying prior proxy usage is indirect: the ledger does not tag the
surface. But ``unillm.set_billing_context`` is called only on assistant
runtime paths and never by the gateway proxy, so proxy spend lands with
``assistant_id IS NULL`` while runtime spend carries an assistant. The
heuristic over-grandfathers rather than under-grandfathers — any runtime
spend predating the billing-context wiring also has a NULL assistant_id —
which is the safe direction: it errs toward not breaking a live caller.

Revision ID: api_access_grandfather
Revises: api_key_kind
Create Date: 2026-08-02 00:01:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "api_access_grandfather"
down_revision = "api_key_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_account",
        sa.Column(
            "api_access_grandfathered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE billing_account
        SET api_access_grandfathered = true
        WHERE id IN (
            SELECT DISTINCT billing_account_id
            FROM credit_transaction
            WHERE category = 'llm'
              AND assistant_id IS NULL
              AND billing_account_id IS NOT NULL
        )
        """,
    )


def downgrade() -> None:
    op.drop_column("billing_account", "api_access_grandfathered")
