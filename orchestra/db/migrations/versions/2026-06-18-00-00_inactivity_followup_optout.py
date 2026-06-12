"""Retire termination grace period; add inactivity-followup opt-out.

Inactivity no longer deletes or deprovisions assistants: the
re-engagement routine only wakes a user's Coordinator to follow up with
them (see ``orchestra.routines.inactivity_followup``), and contact
lifecycle is governed solely by billing suspension. The boss-driven
*termination* path is removed (``termination_initiated_at``); in its
place a boss can opt out of further follow-ups
(``inactivity_followup_opted_out``), which simply excludes their
Coordinator from the routine.

Revision ID: inactivity_followup_optout
Revises: integration_bootstrap_diag
Create Date: 2026-06-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "inactivity_followup_optout"
down_revision = "integration_bootstrap_diag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "inactivity_followup_opted_out",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.drop_index(
        "ix_assistants_termination_initiated_at",
        table_name="assistants",
    )
    op.drop_column("assistants", "termination_initiated_at")


def downgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "termination_initiated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_assistants_termination_initiated_at",
        "assistants",
        ["termination_initiated_at"],
    )
    op.drop_column("assistants", "inactivity_followup_opted_out")
