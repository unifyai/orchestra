"""Add referenced_keys column to active_derived_log_template table

Revision ID: add_ref_keys_derived_tpl
Revises: remove_level_column
Create Date: 2025-12-01 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "add_ref_keys_derived_tpl"
down_revision = "remove_level_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add referenced_keys column for tracking field dependencies
    op.add_column(
        "active_derived_log_template",
        sa.Column(
            "referenced_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,  # Nullable for backward compatibility with existing rows
        ),
    )

    # Create GIN index for fast containment queries
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_active_derived_log_referenced_keys
        ON active_derived_log_template USING GIN (referenced_keys)
        """,
    )


def downgrade() -> None:
    # Drop index first
    op.execute("DROP INDEX IF EXISTS idx_active_derived_log_referenced_keys")
    # Drop column
    op.drop_column("active_derived_log_template", "referenced_keys")
