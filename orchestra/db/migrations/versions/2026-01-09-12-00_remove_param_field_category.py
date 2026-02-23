"""Remove param field category - merge into entry.

Revision ID: remove_param_field_category
Revises: add_phone_number_to_auth_user
Create Date: 2026-01-09 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "remove_param_field_category"
down_revision = "add_phone_number_to_auth_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Change all field_category='param' to 'entry' in field_type table."""
    op.execute(
        "UPDATE field_type SET field_category = 'entry' WHERE field_category = 'param'",
    )


def downgrade() -> None:
    """Non-reversible migration - no way to know which fields were originally 'param'."""
