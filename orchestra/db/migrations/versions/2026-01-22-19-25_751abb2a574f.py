"""add monthly_spending_cap to all spending tables

Revision ID: 751abb2a574f
Revises: add_timezone_to_organization
Create Date: 2026-01-22 19:25:44.695897

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "751abb2a574f"
down_revision = "add_timezone_to_organization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add monthly_spending_cap to all spending-related tables
    op.add_column(
        "assistants",
        sa.Column("monthly_spending_cap", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column("monthly_spending_cap", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column("monthly_spending_cap", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "organization_member",
        sa.Column("monthly_spending_cap", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organization_member", "monthly_spending_cap")
    op.drop_column("organization", "monthly_spending_cap")
    op.drop_column("auth_user", "monthly_spending_cap")
    op.drop_column("assistants", "monthly_spending_cap")
