"""Persist integration bootstrap sync diagnostics.

Revision ID: integration_bootstrap_diagnostics
Revises: integration_bootstrap_state
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "integration_bootstrap_diagnostics"
down_revision = "integration_bootstrap_state"
branch_labels = None
depends_on = None

JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "integration_bootstrap_state",
        sa.Column(
            "last_sync_diagnostics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=JSON_EMPTY_OBJECT,
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("integration_bootstrap_state", "last_sync_diagnostics_json")
