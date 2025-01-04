"""add field_type table

Revision ID: 7e634b76aeaf
Revises: a21317060542
Create Date: 2025-01-03 21:11:40.070058

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "7e634b76aeaf"
down_revision = "a21317060542"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_type",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(), nullable=False),
        sa.Column("field_type", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "field_name", name="uq_project_field_name"),
    )


def downgrade() -> None:
    op.drop_table("field_type")
