"""add_member_roles

Revision ID: 2025-11-07-19-00
Revises: 2025-11-07-18-00
Create Date: 2025-11-07 19:00:00

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_member_roles"
down_revision = "add_rbac_teams_resource_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Phase 4: Add role_id to organization_member table.

    Connects organization membership with RBAC roles, allowing members
    to have specific roles that determine their default permissions.
    """
    # Add role_id column to organization_member
    op.add_column(
        "organization_member",
        sa.Column("role_id", sa.Integer(), nullable=True),
    )

    # Add foreign key constraint
    op.create_foreign_key(
        "fk_organization_member_role_id",
        "organization_member",
        "role",
        ["role_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Set default role for existing members
    # Get the "Member" system role ID and assign to all existing members
    op.execute(
        """
        UPDATE organization_member
        SET role_id = (SELECT id FROM role WHERE name = 'Member' AND organization_id IS NULL LIMIT 1)
        WHERE role_id IS NULL
    """,
    )

    # Set "Owner" role for organization owners
    op.execute(
        """
        UPDATE organization_member om
        SET role_id = (SELECT id FROM role WHERE name = 'Owner' AND organization_id IS NULL LIMIT 1)
        FROM organization o
        WHERE om.organization_id = o.id
        AND om.user_id = o.owner_id
        AND om.role_id = (SELECT id FROM role WHERE name = 'Member' AND organization_id IS NULL LIMIT 1)
    """,
    )


def downgrade() -> None:
    """Remove role_id from organization_member."""
    op.drop_constraint(
        "fk_organization_member_role_id",
        "organization_member",
        type_="foreignkey",
    )
    op.drop_column("organization_member", "role_id")
