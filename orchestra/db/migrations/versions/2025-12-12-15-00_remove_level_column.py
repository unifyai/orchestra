"""Remove level column from organization_member and organization_invite

Revision ID: remove_level_column
Revises: fix_admin_member_roles
Create Date: 2025-12-12 15:00:00.000000

This migration removes the legacy 'level' column from organization_member and
organization_invite tables. The 'level' field (owner/admin/user) has been
superseded by the RBAC 'role_id' field which provides more granular permissions.

After this migration:
- All permission checks use role_id exclusively
- role_id references the Role table (Owner, Admin, Member, Viewer, or custom roles)
- The level field is no longer stored or used
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "remove_level_column"
down_revision = "b8ccd66119a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Remove the legacy 'level' column from organization tables.

    The 'level' column is no longer needed as all permission checks
    now use role_id exclusively via check_org_member_permission().
    """
    # Drop level column from organization_member
    op.drop_column("organization_member", "level")

    # Drop level column from organization_invite
    op.drop_column("organization_invite", "level")


def downgrade() -> None:
    """
    Restore the 'level' column to organization tables.

    Note: This will restore the column but data will need to be
    repopulated based on role_id mapping:
    - Owner role -> 'owner'
    - Admin role -> 'admin'
    - Member/Viewer/other -> 'user'
    """
    # Add level column back to organization_member
    op.add_column(
        "organization_member",
        sa.Column("level", sa.String(), nullable=True),
    )

    # Populate level based on role_id
    op.execute(
        """
        UPDATE organization_member om
        SET level = CASE
            WHEN r.name = 'Owner' THEN 'owner'
            WHEN r.name = 'Admin' THEN 'admin'
            ELSE 'user'
        END
        FROM role r
        WHERE om.role_id = r.id;
        """,
    )

    # Make level NOT NULL after populating
    op.alter_column(
        "organization_member",
        "level",
        nullable=False,
        existing_type=sa.String(),
    )

    # Add level column back to organization_invite
    op.add_column(
        "organization_invite",
        sa.Column("level", sa.String(), nullable=True, server_default="user"),
    )

    # Populate level based on role_id
    op.execute(
        """
        UPDATE organization_invite oi
        SET level = CASE
            WHEN r.name = 'Owner' THEN 'owner'
            WHEN r.name = 'Admin' THEN 'admin'
            ELSE 'user'
        END
        FROM role r
        WHERE oi.role_id = r.id;
        """,
    )

    # Make level NOT NULL after populating
    op.alter_column(
        "organization_invite",
        "level",
        nullable=False,
        existing_type=sa.String(),
    )
