"""Add billing_account.trial_end_at for the card-gated signup trial.

Revision ID: billing_trial_fields
Revises: user_canonical_email
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "billing_trial_fields"
down_revision = "user_canonical_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_account",
        sa.Column("trial_end_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("billing_account", "trial_end_at")
