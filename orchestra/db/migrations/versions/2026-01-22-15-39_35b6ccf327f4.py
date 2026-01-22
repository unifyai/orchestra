"""Add timezone column to organization table

Revision ID: 35b6ccf327f4
Revises: 8b471349c2b6
Create Date: 2026-01-22 15:39:30.261631

This migration adds a timezone column to the organization table.
The timezone is stored as an IANA timezone string (e.g., "America/New_York").
It is initialized from the owner's timezone when the organization is created.

Existing organizations are backfilled with their owner's timezone,
or 'UTC' if the owner doesn't have a timezone set.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_timezone_to_organization"
down_revision = "8b471349c2b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add timezone column to organization table
    op.add_column(
        "organization",
        sa.Column("timezone", sa.String(), nullable=True),
    )

    # Backfill existing organizations with owner's timezone (or UTC if not set)
    op.execute(
        """
        UPDATE organization
        SET timezone = COALESCE(auth_user.timezone, 'UTC')
        FROM auth_user
        WHERE organization.owner_id = auth_user.id
        """,
    )


def downgrade() -> None:
    # Remove timezone column from organization table
    op.drop_column("organization", "timezone")
