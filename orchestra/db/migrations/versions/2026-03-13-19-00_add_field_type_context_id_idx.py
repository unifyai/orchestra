"""Add index on field_type.context_id for cascade delete performance.

Deleting a project cascades through `context`, which in turn cascades through
`field_type` via `field_type.context_id -> context.id`. Without an index on
`field_type.context_id`, PostgreSQL repeatedly scans the full field_type table
for each deleted context, dominating project deletion time.

Revision ID: add_field_type_context_id_idx
Revises: add_lec_context_id_idx
Create Date: 2026-03-13 19:00:00.000000
"""

from alembic import op

revision = "add_field_type_context_id_idx"
down_revision = "add_lec_context_id_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_field_type_context_id",
        "field_type",
        ["context_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_field_type_context_id", table_name="field_type")
