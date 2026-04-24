"""Add assistant_console_config table.

Per-assistant UI/UX configuration for forward-deployed Console views.
One-to-one with ``assistants`` via a unique FK on ``assistant_id``.

Revision ID: add_assistant_console_config
Revises: add_field_type_backfilled_at
Create Date: 2026-04-22 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "add_assistant_console_config"
down_revision = "add_field_type_backfilled_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_console_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "assistant_id",
            sa.Integer,
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("version", sa.String, nullable=False, server_default="1"),
        sa.Column("layout_mode", sa.String, nullable=False, server_default="standard"),
        sa.Column("layout_default_tab", sa.String, nullable=True),
        sa.Column("tabs_hidden", JSONB, nullable=True),
        sa.Column("tabs_order", JSONB, nullable=True),
        sa.Column("theme_brand_name", sa.String, nullable=True),
        sa.Column("theme_accent_color", sa.String, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("assistant_console_config")
