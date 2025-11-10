"""Add RBAC foundation: permissions and roles

Revision ID: add_rbac_foundation
Revises: add_org_billing
Create Date: 2025-11-07 17:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_rbac_foundation"
down_revision = "add_org_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add RBAC foundation tables:
    1. Create permission table (atomic permissions like 'project:read')
    2. Create role table (organizational roles)
    3. Create role_permission join table
    4. Seed default permissions
    5. Seed system roles with permissions
    """
    # Create permission table
    op.create_table(
        "permission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Create role table
    op.create_table(
        "role",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,  # NULL = system role
        ),
        sa.Column(
            "is_system_role",
            sa.Boolean(),
            server_default="f",
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
        "uq_role_name_org",
        "role",
        ["name", "organization_id"],
    )

    # Create role_permission join table
    op.create_table(
        "role_permission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("role.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permission.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Add unique constraint on (role_id, permission_id)
    op.create_unique_constraint(
        "uq_role_permission",
        "role_permission",
        ["role_id", "permission_id"],
    )

    # Seed default permissions
    _seed_permissions()

    # Seed system roles with permissions
    _seed_system_roles()


def downgrade() -> None:
    """
    Revert RBAC foundation tables.
    """
    op.drop_table("role_permission")
    op.drop_table("role")
    op.drop_table("permission")


def _seed_permissions() -> None:
    """Seed default atomic permissions for resources."""
    permissions = [
        # Project permissions
        ("project:read", "View project details", "project", "read"),
        ("project:write", "Edit project", "project", "write"),
        ("project:delete", "Delete project", "project", "delete"),
        # Interface permissions
        ("interface:read", "View interface", "interface", "read"),
        ("interface:write", "Edit interface", "interface", "write"),
        ("interface:delete", "Delete interface", "interface", "delete"),
        # Tab permissions
        ("tab:read", "View tab", "tab", "read"),
        ("tab:write", "Edit tab", "tab", "write"),
        ("tab:delete", "Delete tab", "tab", "delete"),
        # Tile permissions
        ("tile:read", "View tile", "tile", "read"),
        ("tile:write", "Edit tile", "tile", "write"),
        ("tile:delete", "Delete tile", "tile", "delete"),
        # Organization permissions
        ("org:read", "View organization details", "organization", "read"),
        (
            "org:write",
            "Edit organization settings, billing, and members",
            "organization",
            "write",
        ),
        ("org:delete", "Delete organization", "organization", "delete"),
    ]

    for name, description, resource_type, action in permissions:
        op.execute(
            f"""
            INSERT INTO permission (name, description, resource_type, action)
            VALUES ('{name}', '{description}', '{resource_type}', '{action}');
            """,
        )


def _seed_system_roles() -> None:
    """Seed system roles (Owner, Admin, Member, Viewer) with appropriate permissions."""
    # Create Owner role (full access to all resources)
    op.execute(
        """
        INSERT INTO role (name, description, organization_id, is_system_role)
        VALUES ('Owner', 'Full access to all resources and settings', NULL, true);
        """,
    )

    # Assign all permissions to Owner
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true),
            id
        FROM permission;
        """,
    )

    # Create Admin role (all permissions except org:delete - only owner can delete org)
    op.execute(
        """
        INSERT INTO role (name, description, organization_id, is_system_role)
        VALUES ('Admin', 'Full access except deleting organization', NULL, true);
        """,
    )

    # Assign permissions to Admin (all except org:delete)
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Admin' AND is_system_role = true),
            id
        FROM permission
        WHERE name != 'org:delete';
        """,
    )

    # Create Member role (read/write content and org settings, no delete)
    op.execute(
        """
        INSERT INTO role (name, description, organization_id, is_system_role)
        VALUES ('Member', 'Create and edit content and organization settings, cannot delete', NULL, true);
        """,
    )

    # Assign permissions to Member (read/write for all resources, no delete)
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Member' AND is_system_role = true),
            id
        FROM permission
        WHERE action IN ('read', 'write');
        """,
    )

    # Create Viewer role (read-only access)
    op.execute(
        """
        INSERT INTO role (name, description, organization_id, is_system_role)
        VALUES ('Viewer', 'Read-only access to organization resources', NULL, true);
        """,
    )

    # Assign permissions to Viewer (read-only)
    op.execute(
        """
        INSERT INTO role_permission (role_id, permission_id)
        SELECT
            (SELECT id FROM role WHERE name = 'Viewer' AND is_system_role = true),
            id
        FROM permission
        WHERE action = 'read';
        """,
    )
