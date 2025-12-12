"""add log_event_version table for JSONB versioning

Revision ID: 7f6be7eb120c
Revises: add_key_order_log_event
Create Date: 2025-12-11 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "7f6be7eb120c"
down_revision = "add_key_order_log_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create log_event_version table for JSONB versioning
    # Unlike log_version (EAV mode), this stores complete JSONB documents per event
    op.create_table(
        "log_event_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("context_version_id", sa.Integer(), nullable=False),
        sa.Column("log_event_id", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("key_order", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(
            ["context_version_id"],
            ["context_version.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes for query performance
    op.create_index(
        op.f("ix_log_event_version_context_version_id"),
        "log_event_version",
        ["context_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_log_event_version_log_event_id"),
        "log_event_version",
        ["log_event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_log_event_version_log_event_id"),
        table_name="log_event_version",
    )
    op.drop_index(
        op.f("ix_log_event_version_context_version_id"),
        table_name="log_event_version",
    )
    op.drop_table("log_event_version")
