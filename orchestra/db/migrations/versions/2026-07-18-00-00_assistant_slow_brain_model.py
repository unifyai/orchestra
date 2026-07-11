"""Add per-assistant slow-brain LLM columns to the assistants table.

Stores the ConversationManager slow-brain LLM as a unillm ``model@provider``
endpoint string plus a reasoning-effort level. NULL means the platform slow-
brain default (``SLOW_BRAIN_MODEL`` / GPT-5.6 Terra) applies.

Revision ID: assistant_slow_brain_model
Revises: clear_managed_desktop_gf
Create Date: 2026-07-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "assistant_slow_brain_model"
down_revision = "clear_managed_desktop_gf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("slow_brain_model", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("slow_brain_reasoning_effort", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistants", "slow_brain_reasoning_effort")
    op.drop_column("assistants", "slow_brain_model")
