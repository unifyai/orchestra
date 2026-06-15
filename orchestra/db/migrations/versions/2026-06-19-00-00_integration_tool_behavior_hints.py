"""Persist normalized integration tool behavior hints.

Revision ID: integration_tool_behavior_hints
Revises: inactivity_followup_optout
Create Date: 2026-06-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "integration_tool_behavior_hints"
down_revision = "inactivity_followup_optout"
branch_labels = None
depends_on = None

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.add_column(
        "provider_tool_catalog",
        sa.Column(
            "behavior_hints_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_tool_catalog", "behavior_hints_json")
