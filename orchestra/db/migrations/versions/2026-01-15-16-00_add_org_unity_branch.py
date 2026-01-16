"""Add unity_branch column to organization table

Revision ID: add_org_unity_branch
Revises: drop_legacy_tables
Create Date: 2026-01-15 16:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_org_unity_branch"
down_revision = "drop_legacy_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add unity_branch column to organization table.

    This column specifies which Unity branch to use for this org's deployments.
    NULL means use standard main/staging branches. Only set for orgs with
    custom branches (e.g., "client/midland-heart", "colliers").
    """
    op.add_column(
        "organization",
        sa.Column("unity_branch", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_organization_unity_branch",
        "organization",
        ["unity_branch"],
    )


def downgrade() -> None:
    """Remove unity_branch column from organization table."""
    op.drop_index("ix_organization_unity_branch", table_name="organization")
    op.drop_column("organization", "unity_branch")
