"""Add phone_number column to auth_user table

Revision ID: add_phone_number_to_auth_user
Revises: add_queue_processing_time
Create Date: 2025-12-30 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_phone_number_to_auth_user"
down_revision = "add_queue_processing_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add phone_number column to auth_user table."""
    op.add_column(
        "auth_user",
        sa.Column("phone_number", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Remove phone_number column from auth_user table."""
    op.drop_column("auth_user", "phone_number")
