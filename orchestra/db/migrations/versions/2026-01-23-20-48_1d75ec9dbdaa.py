"""Change embedding.ref_id FK from CASCADE to SET NULL

This preserves soft-deleted embeddings when parent LogEvent is deleted,
avoiding expensive HNSW index surgery on every delete operation.

Revision ID: 1d75ec9dbdaa
Revises: 751abb2a574f
Create Date: 2026-01-23 20:48:27.757715

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "1d75ec9dbdaa"
down_revision = "751abb2a574f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make ref_id nullable and change FK from CASCADE to SET NULL
    op.alter_column("embedding", "ref_id", existing_type=sa.INTEGER(), nullable=True)
    op.drop_constraint("embedding_ref_id_fkey", "embedding", type_="foreignkey")
    op.create_foreign_key(
        "embedding_ref_id_fkey",
        "embedding",
        "log_event",
        ["ref_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Revert to CASCADE and NOT NULL
    op.drop_constraint("embedding_ref_id_fkey", "embedding", type_="foreignkey")
    op.create_foreign_key(
        "embedding_ref_id_fkey",
        "embedding",
        "log_event",
        ["ref_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("embedding", "ref_id", existing_type=sa.INTEGER(), nullable=False)
