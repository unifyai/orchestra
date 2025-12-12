"""Add key_order JSONB column to log_event table for preserving dict key ordering

Revision ID: add_key_order_log_event
Revises: a2299418c4c9
Create Date: 2025-12-09 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "add_key_order_log_event"
down_revision = "a2299418c4c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add key_order column for preserving nested dictionary key insertion order
    op.add_column(
        "log_event",
        sa.Column(
            "key_order",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,  # Nullable for backward compatibility with existing logs
        ),
    )


def downgrade() -> None:
    op.drop_column("log_event", "key_order")
