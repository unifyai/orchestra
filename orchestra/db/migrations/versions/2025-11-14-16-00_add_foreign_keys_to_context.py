"""Add foreign_keys column to context table

Revision ID: add_foreign_keys_to_context
Revises: 2b35f76ca925
Create Date: 2025-11-14 16:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "add_foreign_keys_to_context"
down_revision = "add_safe_temporal_cast_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add foreign_keys column to context table with default empty array
    op.add_column(
        "context",
        sa.Column(
            "foreign_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    # Remove foreign_keys column from context table
    op.drop_column("context", "foreign_keys")
