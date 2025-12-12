"""Fix admin member roles

Revision ID: fix_admin_member_roles
Revises: 9898c8469548
Create Date: 2025-12-12 12:00:00.000000

This migration fixes a gap in the original add_member_roles migration where
members with level='admin' were not assigned the Admin role. They were
incorrectly given the Member role, which doesn't have org:write permission.

This migration updates:
- level='admin' members → Admin role (has org:write)
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "fix_admin_member_roles"
down_revision = "9898c8469548"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Fix existing admin members to have Admin role instead of Member role.

    This addresses the issue where members added with level='admin' were
    assigned the Member role (which only has org:read), instead of the
    Admin role (which has org:write).
    """
    # Update members with level='admin' who have Member role to Admin role
    op.execute(
        """
        UPDATE organization_member
        SET role_id = (
            SELECT id FROM role
            WHERE name = 'Admin' AND organization_id IS NULL
            LIMIT 1
        )
        WHERE level = 'admin'
        AND role_id = (
            SELECT id FROM role
            WHERE name = 'Member' AND organization_id IS NULL
            LIMIT 1
        );
        """,
    )


def downgrade() -> None:
    """
    Revert admin members back to Member role.

    Note: This is a lossy operation - we cannot distinguish between members
    who were originally admin vs those who were later upgraded.
    """
    # Revert Admin role members with level='admin' back to Member role
    op.execute(
        """
        UPDATE organization_member
        SET role_id = (
            SELECT id FROM role
            WHERE name = 'Member' AND organization_id IS NULL
            LIMIT 1
        )
        WHERE level = 'admin'
        AND role_id = (
            SELECT id FROM role
            WHERE name = 'Admin' AND organization_id IS NULL
            LIMIT 1
        );
        """,
    )
