"""Add per-assistant workspace file access allowlist.

Revision ID: assistant_workspace_file_access
Revises: fix_contact_value_idx_predicate
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "assistant_workspace_file_access"
down_revision = "fix_contact_value_idx_predicate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_workspace_file_access",
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column(
            "default_allow",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "decisions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["assistants.agent_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("agent_id", "provider"),
    )


def downgrade() -> None:
    op.drop_table("assistant_workspace_file_access")
