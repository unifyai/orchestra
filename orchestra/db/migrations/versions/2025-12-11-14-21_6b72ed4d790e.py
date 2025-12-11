"""seed_billing_permissions

Revision ID: 6b72ed4d790e
Revises: 621d5dcac748
Create Date: 2025-12-11 14:21:15.502786

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "6b72ed4d790e"
down_revision = "621d5dcac748"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add billing permissions (idempotent - skip if already exists)
    op.execute(
        """
        INSERT INTO permission (name, description, resource_type, action)
        VALUES
            ('billing:read', 'View billing information, credits, and invoices', 'billing', 'read'),
            ('billing:write', 'Update billing settings, autorecharge, and business profile', 'billing', 'write')
        ON CONFLICT (name) DO NOTHING;
    """
    )

    # Owner gets all permissions (billing:read and billing:write)
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM role r, permission p
        WHERE r.name = 'Owner' AND r.is_system_role = true
          AND p.name IN ('billing:read', 'billing:write')
        ON CONFLICT DO NOTHING;
    """
    )

    # Admin gets billing:read and billing:write
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM role r, permission p
        WHERE r.name = 'Admin' AND r.is_system_role = true
          AND p.name IN ('billing:read', 'billing:write')
        ON CONFLICT DO NOTHING;
    """
    )

    # Member gets billing:read only
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM role r, permission p
        WHERE r.name = 'Member' AND r.is_system_role = true
          AND p.name = 'billing:read'
        ON CONFLICT DO NOTHING;
    """
    )

    # Viewer gets billing:read only
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM role r, permission p
        WHERE r.name = 'Viewer' AND r.is_system_role = true
          AND p.name = 'billing:read'
        ON CONFLICT DO NOTHING;
    """
    )


def downgrade() -> None:
    # Remove role_permission assignments for billing permissions
    op.execute(
        """
        DELETE FROM role_permission
        WHERE permission_id IN (
            SELECT id FROM permission WHERE name IN ('billing:read', 'billing:write')
        );
    """
    )

    # Remove billing permissions
    op.execute(
        """
        DELETE FROM permission WHERE name IN ('billing:read', 'billing:write');
    """
    )
