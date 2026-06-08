"""Drop the dead ``billing_account.tier`` column.

``tier`` (a free-string ``developer``/``professional``/``enterprise`` flag)
predates the managed-billing v2 plan model and is now fully redundant with
``BillingPlanTemplate`` + ``BillingPlanAssignment`` (the real subscription
tier, resolved via ``plan_assignment_id``). The column was *write-only*: the
admin ``PUT /billing/tier`` endpoint (also removed) set it but nothing ever
read it for behaviour, an API response, or the console.

Independent of the self-serve subscription model — kept as its own
migration so it can be reasoned about / reverted on its own — but chained
after it to keep a single linear head.

Revision ID: drop_billing_account_tier
Revises: self_serve_billing
Create Date: 2026-06-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_billing_account_tier"
down_revision = "self_serve_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("billing_account", "tier")


def downgrade() -> None:
    op.add_column(
        "billing_account",
        sa.Column(
            "tier",
            sa.String(),
            nullable=False,
            server_default="developer",
        ),
    )
