"""Add reactions JSONB column to dm_message.

Revision ID: dm_message_reactions
Revises: integration_app_preferences
Create Date: 2026-08-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "dm_message_reactions"
down_revision = "integration_app_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dm_message",
        sa.Column(
            "reactions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dm_message", "reactions")
