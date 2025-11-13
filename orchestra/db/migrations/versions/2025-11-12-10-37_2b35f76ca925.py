"""Rename region to nationality and country to phone_country in Assistant table

Revision ID: 2b35f76ca925
Revises: add_member_roles
Create Date: 2025-11-12 10:37:28.986905

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "2b35f76ca925"
down_revision = "add_member_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use alter_column with the new_column_name parameter to preserve data
    op.alter_column("assistants", "country", new_column_name="phone_country")
    op.alter_column("assistants", "region", new_column_name="nationality")


def downgrade() -> None:
    # Reverse the rename operations
    op.alter_column("assistants", "phone_country", new_column_name="country")
    op.alter_column("assistants", "nationality", new_column_name="region")
