"""Remove 256-char limit on field_type.description column.

Revision ID: remove_field_desc_limit
Revises: add_api_messages
Create Date: 2026-03-07 20:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "remove_field_desc_limit"
down_revision = "add_api_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "field_type",
        "description",
        type_=sa.String(),
        existing_type=sa.String(256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "field_type",
        "description",
        type_=sa.String(256),
        existing_type=sa.String(),
        existing_nullable=True,
    )
