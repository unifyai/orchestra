"""Add inactivity follow-up series/stage/thread tracking.

Revision ID: inactivity_followup_series
Revises: assistant_peer_dm_threads
Create Date: 2026-07-26 22:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "inactivity_followup_series"
down_revision = "assistant_peer_dm_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "inactivity_followup_series",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "inactivity_followup_stage",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "inactivity_followup_thread_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "inactivity_followup_has_engaged",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "inactivity_followup_has_engaged")
    op.drop_column("assistants", "inactivity_followup_thread_ids")
    op.drop_column("assistants", "inactivity_followup_stage")
    op.drop_column("assistants", "inactivity_followup_series")
