"""Add founder interview ask tracking on assistants.

Revision ID: founder_interview_ask
Revises: inactivity_followup_series
Create Date: 2026-07-26 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "founder_interview_ask"
down_revision = "inactivity_followup_series"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "founder_interview_asked_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "founder_interview_ask_variant",
            sa.String(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "founder_interview_ask_variant")
    op.drop_column("assistants", "founder_interview_asked_at")
