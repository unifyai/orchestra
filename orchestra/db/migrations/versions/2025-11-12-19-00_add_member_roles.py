"""add_member_roles

Revision ID: add_member_roles
Revises: add_rbac_teams_resource_access
Create Date: 2025-11-12 19:00:00.000000

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

    Enforces explicit role assignment:
    - All members MUST have a role_id (NOT NULL)
    - Defaults to Member role for existing members
    - Organization owners get Owner role
    - ondelete="RESTRICT" prevents deleting in-use roles
    """
    op.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE pid != pg_backend_pid()           -- Don't kill our own session
          AND datname = current_database()       -- Only this database
          AND usename != 'cloudsqlsuperuser';    -- Don't kill Cloud SQL admin
        """,
    )

    # Add role_id column to organization_member (initially nullable for data migration)
    op.add_column(
        "organization_member",
        sa.Column("role_id", sa.Integer(), nullable=True),
    )

    # Add foreign key constraint with RESTRICT (temporary, will be recreated)
    op.create_foreign_key(
        "fk_organization_member_role_id",
        "organization_member",
        "role",
        ["role_id"],
        ["id"],
        ondelete="RESTRICT",
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

    # Now that all members have role_id set, make it NOT NULL
    op.alter_column(
        "organization_member",
        "role_id",
        nullable=False,
        existing_type=sa.Integer(),
    )


def downgrade() -> None:
    """Remove role_id from organization_member."""
    # Make column nullable before dropping (for safe downgrade)
    op.alter_column(
        "organization_member",
        "role_id",
        nullable=True,
        existing_type=sa.Integer(),
    )

    # Drop foreign key constraint
    op.drop_constraint(
        "fk_organization_member_role_id",
        "organization_member",
        type_="foreignkey",
    )

    # Drop column
    op.drop_column("organization_member", "role_id")
