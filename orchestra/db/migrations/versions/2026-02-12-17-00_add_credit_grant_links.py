"""Add credit_amount and rename approval links to credit grant links.

Converts one-time approval links into credit grant links:
1. Adds credit_amount column to specify credits granted when claimed
2. Renames table: assistant_hiring_one_time_approval_link → one_time_credit_grant_link
3. Renames user field: has_claimed_approval_link → has_claimed_credit_grant_link

Revision ID: credit_grant_links_001
Revises: rate_limit_counter_001
Create Date: 2026-02-12 17:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

from orchestra.settings import settings

# revision identifiers, used by Alembic.
revision = "credit_grant_links_001"
down_revision = "rate_limit_counter_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add credit_amount column and rename table/column."""
    # Step 1: Add credit_amount column to existing table
    op.add_column(
        "assistant_hiring_one_time_approval_link",
        sa.Column(
            "credit_amount",
            sa.Float(),
            nullable=False,
            server_default=str(settings.assistant_creation_cost),
            comment="Amount of credits to grant when link is claimed",
        ),
    )

    # Remove server_default after column is created (keep it in code only)
    op.alter_column(
        "assistant_hiring_one_time_approval_link",
        "credit_amount",
        server_default=None,
    )

    # Step 2: Rename the table
    op.rename_table(
        "assistant_hiring_one_time_approval_link",
        "one_time_credit_grant_link",
    )

    # Step 3: Rename the user field
    op.alter_column(
        "auth_user",
        "has_claimed_approval_link",
        new_column_name="has_claimed_credit_grant_link",
    )


def downgrade() -> None:
    """Revert all changes."""
    # Step 1: Revert user field name
    op.alter_column(
        "auth_user",
        "has_claimed_credit_grant_link",
        new_column_name="has_claimed_approval_link",
    )

    # Step 2: Revert table name
    op.rename_table(
        "one_time_credit_grant_link",
        "assistant_hiring_one_time_approval_link",
    )

    # Step 3: Remove credit_amount column
    op.drop_column("assistant_hiring_one_time_approval_link", "credit_amount")
