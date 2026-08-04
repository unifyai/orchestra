"""Drop the plan pricing factors.

Usage bills at list price. ``billing_plan_template`` carried two multipliers
on raw usage — ``base_pricing_factor`` for every unit and
``overage_pricing_factor`` as a further uplift above commit — and the
platform no longer prices that way, so both columns and the constraint
keeping them positive go.

Every row held 1.0 for both, so dropping them changes no invoice: the
invoicer's arithmetic reduces to the identity it was already computing.
Historical ``recharge.detail`` audit blobs keep whatever factors they
recorded; they are a record of what was charged at the time and are not
rewritten here.

Revision ID: drop_plan_pricing_factors
Revises: canvas_token
Create Date: 2026-08-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_plan_pricing_factors"
down_revision = "canvas_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_plan_template_pricing_factors_positive",
        "billing_plan_template",
        type_="check",
    )
    op.drop_column("billing_plan_template", "overage_pricing_factor")
    op.drop_column("billing_plan_template", "base_pricing_factor")


def downgrade() -> None:
    op.add_column(
        "billing_plan_template",
        sa.Column(
            "base_pricing_factor",
            sa.Numeric(),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.add_column(
        "billing_plan_template",
        sa.Column(
            "overage_pricing_factor",
            sa.Numeric(),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.create_check_constraint(
        "ck_plan_template_pricing_factors_positive",
        "billing_plan_template",
        "base_pricing_factor > 0 AND overage_pricing_factor > 0",
    )
