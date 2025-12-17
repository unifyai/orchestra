"""Add assistant permissions to RBAC system

This migration adds assistant-specific permissions to the existing RBAC system:
- assistant:read - View assistant details
- assistant:write - Create/edit assistants
- assistant:delete - Delete assistants

Role assignments:
- Owner: all permissions (read, write, delete)
- Admin: all permissions (read, write, delete)
- Member: read, write (can create/edit but not delete)
- Viewer: read only

Revision ID: add_assistant_permissions
Revises: add_org_to_assistant
Create Date: 2025-12-16 12:01:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "add_assistant_permissions"
down_revision = "add_org_to_assistant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add assistant permissions and assign them to system roles.

    Permissions:
    - assistant:read - View assistant details
    - assistant:write - Create/edit assistants
    - assistant:delete - Delete assistants

    Role assignments follow the existing pattern:
    - Owner: all permissions
    - Admin: all permissions
    - Member: read + write
    - Viewer: read only
    """
    # Step 1: Insert assistant permissions
    op.execute(
        """
        INSERT INTO permission (name, description, resource_type, action)
        VALUES
            ('assistant:read', 'View assistant details', 'assistant', 'read'),
            ('assistant:write', 'Create and edit assistants', 'assistant', 'write'),
            ('assistant:delete', 'Delete assistants', 'assistant', 'delete');
        """,
    )

    # Step 2: Assign all assistant permissions to Owner role
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true),
            id
        FROM permission
        WHERE resource_type = 'assistant';
        """,
    )

    # Step 3: Assign all assistant permissions to Admin role
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Admin' AND is_system_role = true),
            id
        FROM permission
        WHERE resource_type = 'assistant';
        """,
    )

    # Step 4: Assign read + write permissions to Member role
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Member' AND is_system_role = true),
            id
        FROM permission
        WHERE resource_type = 'assistant' AND action IN ('read', 'write');
        """,
    )

    # Step 5: Assign read permission to Viewer role
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Viewer' AND is_system_role = true),
            id
        FROM permission
        WHERE resource_type = 'assistant' AND action = 'read';
        """,
    )


def downgrade() -> None:
    """
    Remove assistant permissions from the RBAC system.
    """
    # Step 1: Remove role_permission assignments for assistant permissions
    op.execute(
        """
        DELETE FROM role_permission
        WHERE permission_id IN (
            SELECT id FROM permission WHERE resource_type = 'assistant'
        );
        """,
    )

    # Step 2: Remove assistant permissions
    op.execute(
        """
        DELETE FROM permission WHERE resource_type = 'assistant';
        """,
    )
