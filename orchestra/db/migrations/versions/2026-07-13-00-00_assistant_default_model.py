"""Add per-assistant default LLM columns to the assistants table.

Stores the assistant's default LLM as a unillm ``model@provider`` endpoint
string plus a reasoning-effort level. NULL means the platform default
(``UNIFY_MODEL``) applies.

Revision ID: assistant_default_model
Revises: 2026_ms_teams_bot_integration
Create Date: 2026-07-13 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "assistant_default_model"
down_revision = "2026_ms_teams_bot_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("default_model", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("default_reasoning_effort", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistants", "default_reasoning_effort")
    op.drop_column("assistants", "default_model")
