"""Add monthly_spending_cap_set_at columns to entity tables.

This migration adds a timestamp column to track when the spending cap was last
changed. This is used to detect the "limit removed then re-enabled" scenario:

Affected tables:
- assistants (assistant spending cap)
- auth_user (user personal spending cap)
- organization_member (member spending cap within org)
- organization (org-wide spending cap)

Revision ID: 8a1b2c3d4e5f
Revises: refactor_user_desktop_fields
Create Date: 2026-02-04 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8a1b2c3d4e5f"
down_revision = "refactor_user_desktop_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add to assistants table
    op.add_column(
        "assistants",
        sa.Column(
            "monthly_spending_cap_set_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the spending cap was last changed",
        ),
    )

    # Add to auth_user table
    op.add_column(
        "auth_user",
        sa.Column(
            "monthly_spending_cap_set_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the spending cap was last changed",
        ),
    )

    # Add to organization_member table
    op.add_column(
        "organization_member",
        sa.Column(
            "monthly_spending_cap_set_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the spending cap was last changed",
        ),
    )

    # Add to organization table
    op.add_column(
        "organization",
        sa.Column(
            "monthly_spending_cap_set_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the spending cap was last changed",
        ),
    )


def downgrade() -> None:
    op.drop_column("organization", "monthly_spending_cap_set_at")
    op.drop_column("organization_member", "monthly_spending_cap_set_at")
    op.drop_column("auth_user", "monthly_spending_cap_set_at")
    op.drop_column("assistants", "monthly_spending_cap_set_at")
