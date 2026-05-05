"""Add context_counter table.

Stores materialized auto-counter state for ``LogEventDAO.get_next_composite_ids``.
The previous hot path repeatedly scanned every log_event in a context to compute
``max(data->>counter_column)``. This table lets each counter tuple pay that scan
once, then use a primary-key update for subsequent bumps.

Revision ID: add_context_counter
Revises: add_assistant_console_config
Create Date: 2026-04-24 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "add_context_counter"
down_revision = "add_assistant_console_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_counter",
        sa.Column(
            "context_id",
            sa.Integer,
            sa.ForeignKey("context.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("column_name", sa.Text, nullable=False),
        sa.Column("parent_values_hash", sa.Text, nullable=False),
        sa.Column("parent_values", JSONB, nullable=False),
        sa.Column("next_value", sa.BigInteger, nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "context_id",
            "column_name",
            "parent_values_hash",
            name="pk_context_counter",
        ),
    )


def downgrade() -> None:
    op.drop_table("context_counter")
