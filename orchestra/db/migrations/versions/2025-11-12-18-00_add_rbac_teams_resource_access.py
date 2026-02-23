"""Add RBAC teams and resource access

Revision ID: add_rbac_teams_resource_access
Revises: add_rbac_foundation
Create Date: 2025-11-12 18:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_rbac_teams_resource_access"
down_revision = "add_rbac_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add RBAC application tables:
    1. Create team table (teams within organizations)
    2. Create team_member join table
    3. Create resource_access table (for granular permissions)
    """
    # Create team table
    op.create_table(
        "team",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Add unique constraint on (name, organization_id)
    op.create_unique_constraint(
        "uq_team_name_org",
        "team",
        ["name", "organization_id"],
    )

    # Create team_member join table
    op.create_table(
        "team_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "team_id",
            sa.Integer(),
            sa.ForeignKey("team.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Add unique constraint on (team_id, user_id)
    op.create_unique_constraint(
        "uq_team_member",
        "team_member",
        ["team_id", "user_id"],
    )

    # Create resource_access table
    op.create_table(
        "resource_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("role.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grantee_type", sa.String(), nullable=False),
        sa.Column("grantee_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Add unique constraint on (resource_type, resource_id, role_id, grantee_type, grantee_id)
    op.create_unique_constraint(
        "uq_resource_access",
        "resource_access",
        ["resource_type", "resource_id", "role_id", "grantee_type", "grantee_id"],
    )

    # Add indexes for resource_access lookups
    op.create_index(
        "idx_resource_access_resource",
        "resource_access",
        ["resource_type", "resource_id"],
    )

    op.create_index(
        "idx_resource_access_grantee",
        "resource_access",
        ["grantee_type", "grantee_id"],
    )


def downgrade() -> None:
    """
    Revert RBAC application tables.
    """
    op.drop_table("resource_access")
    op.drop_table("team_member")
    op.drop_table("team")
