"""Add log_unique_constraint table for efficient unique field validation.

This migration creates a lookup table that replaces O(N×M) JSONB containment
scans with O(M×log N) B-tree index lookups for unique field validation.

The table uses a composite primary key (context_id, field_name, value_hash)
which provides:
- Automatic uniqueness enforcement via database constraint
- O(log N) lookups via B-tree index on the primary key
- Efficient batch inserts with ON CONFLICT DO NOTHING

Migration Strategy:
1. Deploy code with ORCHESTRA_UNIQUE_VALIDATION_MODE=jsonb_scan (default)
2. Run this migration to create the table
3. Run backfill script to populate existing constraints
4. Set ORCHESTRA_UNIQUE_VALIDATION_MODE=lookup_table to enable fast path

Revision ID: 7f8e9a1b2c3d
Revises: a9d21cb31092
Create Date: 2026-01-27 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "7f8e9a1b2c3d"
down_revision = "a9d21cb31092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the lookup table for unique field constraints
    op.create_table(
        "log_unique_constraint",
        sa.Column("context_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(), nullable=False),
        sa.Column("value_hash", sa.String(32), nullable=False),  # MD5 hex = 32 chars
        sa.Column("log_event_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        # Composite primary key provides uniqueness + B-tree index
        sa.PrimaryKeyConstraint("context_id", "field_name", "value_hash"),
        # Foreign key with CASCADE delete - when log is deleted, constraint is removed
        sa.ForeignKeyConstraint(
            ["log_event_id"],
            ["log_event.id"],
            name="log_unique_constraint_log_event_id_fkey",
            ondelete="CASCADE",
        ),
    )

    # Index for efficient cleanup when logs are deleted
    # (CASCADE handles this, but useful for manual cleanup queries)
    op.create_index(
        "idx_log_unique_constraint_log_event",
        "log_unique_constraint",
        ["log_event_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_log_unique_constraint_log_event")
    op.drop_table("log_unique_constraint")
