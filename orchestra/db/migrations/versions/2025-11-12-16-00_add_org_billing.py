"""Add organization billing support

Revision ID: add_org_billing
Revises: 1cb930843ee0
Create Date: 2025-11-12 16:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_org_billing"
down_revision = "1cb930843ee0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add organization billing support:
    1. Drop unique constraint on organization.owner_id (allow multiple orgs per user)
    2. Add billing_user_id to organization table
    3. Add organization_id to query table for tracking org vs personal queries
    4. Backfill existing organizations with billing_user_id = owner_id
    """
    # Increase lock timeout to 60 seconds to handle live database modifications
    op.execute("SET lock_timeout = '60s'")

    # Drop the unique constraint on owner_id (users can own multiple organizations)
    op.drop_constraint(
        "organization_owner_id_key",
        "organization",
        type_="unique",
    )

    # Add billing_user_id column to organization table
    op.add_column(
        "organization",
        sa.Column(
            "billing_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,  # Temporarily nullable for migration
        ),
    )

    # Backfill existing organizations: set billing_user_id = owner_id
    op.execute(
        """
        UPDATE organization
        SET billing_user_id = owner_id
        WHERE billing_user_id IS NULL;
        """,
    )

    # Now make billing_user_id NOT NULL
    op.alter_column(
        "organization",
        "billing_user_id",
        nullable=False,
    )

    # Add organization_id column to query table (nullable - NULL means personal query)
    op.add_column(
        "query",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,  # NULL = personal query
        ),
    )

    # Add index for organization_id filtering
    op.create_index(
        "ix_query_organization_id",
        "query",
        ["organization_id"],
    )


def downgrade() -> None:
    """
    Revert organization billing support.
    """
    # Drop index and column from query table
    op.drop_index("ix_query_organization_id", table_name="query")
    op.drop_column("query", "organization_id")

    # Drop billing_user_id from organization table
    op.drop_column("organization", "billing_user_id")

    # Restore unique constraint on owner_id
    op.create_unique_constraint(
        "organization_owner_id_key",
        "organization",
        ["owner_id"],
    )
