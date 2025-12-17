"""Add organization_id to assistants table

This migration adds organization support for assistants. Assistants can now be:
- Personal: organization_id is NULL, owned by a single user
- Organizational: organization_id is set, belongs to an organization

Changes:
1. Add organization_id column (FK to organization, nullable)
2. Add index for efficient filtering by organization
3. Add unique constraint for org assistants (organization_id, first_name, surname)

Revision ID: add_org_to_assistant
Revises: single_role_per_resource
Create Date: 2025-12-16 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_org_to_assistant"
down_revision = "single_role_per_resource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add organization support to assistants table.

    1. Add organization_id column with FK to organization
    2. Add index for filtering by organization
    3. Add unique constraint for org assistants
    """
    # Step 1: Add organization_id column
    op.add_column(
        "assistants",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # Step 2: Add index for efficient filtering
    op.create_index(
        "ix_assistants_organization_id",
        "assistants",
        ["organization_id"],
    )

    # Step 3: Add unique constraint for org assistants
    # This ensures no duplicate names within an organization
    # Note: PostgreSQL treats NULLs as distinct, so this constraint only
    # applies when organization_id is NOT NULL
    op.create_unique_constraint(
        "uq_org_assistant_name",
        "assistants",
        ["organization_id", "first_name", "surname"],
    )


def downgrade() -> None:
    """
    Remove organization support from assistants table.
    """
    # Drop unique constraint
    op.drop_constraint("uq_org_assistant_name", "assistants", type_="unique")

    # Drop index
    op.drop_index("ix_assistants_organization_id", table_name="assistants")

    # Drop column
    op.drop_column("assistants", "organization_id")
