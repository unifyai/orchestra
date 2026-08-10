"""Reclassify comped credits mislabelled as collected payments.

Every real customer charge flows through Stripe, which requires a Stripe
customer on the billing account — so a ``payment``-typed PAID recharge on
an account with no ``stripe_customer_id`` cannot be money the platform
collected. Internal comp grants (the $1,000 *Unify* and $500 *Client Beta*
org recharges) were recorded this way, inflating apparent revenue roughly
fourfold. Retype them as ``promo`` so revenue reporting counts only
Stripe-backed collections.

Revision ID: comp_recharges_are_promo
Revises: ms_teams_route_sender
Create Date: 2026-08-10 01:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "comp_recharges_are_promo"
down_revision = "ms_teams_route_sender"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            UPDATE recharge
            SET type = 'promo'
            WHERE type = 'payment'
              AND status = 'PAID'
              AND billing_account_id IN (
                SELECT id FROM billing_account WHERE stripe_customer_id IS NULL
              )
            RETURNING id, billing_account_id, quantity
            """,
        ),
    ).fetchall()
    for row in rows:
        print(
            f"reclassified recharge id={row.id} "
            f"billing_account_id={row.billing_account_id} "
            f"quantity={row.quantity} payment -> promo",
        )


def downgrade() -> None:
    # The 'payment' labelling was the defect; there is nothing to restore.
    pass
