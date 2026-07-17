"""Add ui_editable flag to field_type.

Revision ID: field_type_ui_editable
Revises: unified_chat_store
Create Date: 2026-08-06 00:00:00.000000
"""

from alembic import op

revision = "field_type_ui_editable"
down_revision = "unified_chat_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE field_type
        ADD COLUMN IF NOT EXISTS ui_editable BOOLEAN NOT NULL DEFAULT true
        """,
    )


def downgrade() -> None:
    op.execute("ALTER TABLE field_type DROP COLUMN IF EXISTS ui_editable")
