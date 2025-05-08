"""Create pgvector extension

Revision ID: add_pgvector_extension
Revises: 46c2450d64eb
Create Date: 2025-05-07 00:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_pgvector_extension"
down_revision = "46c2450d64eb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the pgvector extension for vector support
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def downgrade() -> None:
    # Drop the pgvector extension
    op.execute("DROP EXTENSION IF EXISTS vector;")
