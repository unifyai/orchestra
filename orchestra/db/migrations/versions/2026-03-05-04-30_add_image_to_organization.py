"""Add image column to organization table.

Revision ID: add_org_image
Revises: 2026-03-04-12-00_fix_assistant_name_unique_constraint
Create Date: 2026-03-05 04:30:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_org_image"
down_revision = "fix_asst_name_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("image", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("organization", "image")
