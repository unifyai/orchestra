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

    Uses production-safe approach with minimal locking:
    - Adds column without constraints first (brief lock)
    - Adds foreign key as NOT VALID (no table scan required)
    - Populates data
    - Validates constraint separately (allows concurrent reads/writes)
    - Finally makes column NOT NULL (brief lock, data already populated)
    """
    # Step 1: Add column with default NULL (requires brief ACCESS EXCLUSIVE lock)
    # PostgreSQL can add a nullable column without rewriting the table
    op.execute(
        """
        ALTER TABLE organization_member
        ADD COLUMN IF NOT EXISTS role_id INTEGER DEFAULT NULL;
        """,
    )

    # Step 2: Add foreign key constraint as NOT VALID
    # This allows the constraint to be added without validating existing rows
    # Uses only SHARE ROW EXCLUSIVE lock, which doesn't block reads/writes
    op.execute(
        """
        ALTER TABLE organization_member
        ADD CONSTRAINT fk_organization_member_role_id
        FOREIGN KEY (role_id) REFERENCES role(id)
        ON DELETE RESTRICT
        NOT VALID;
        """,
    )

    # Step 3: Populate data - Set default role for existing members
    # Get the "Member" system role ID and assign to all existing members
    op.execute(
        """
        UPDATE organization_member
        SET role_id = (
            SELECT id FROM role
            WHERE name = 'Member'
            AND organization_id IS NULL
            LIMIT 1
        )
        WHERE role_id IS NULL;
        """,
    )

    # Step 4: Set "Owner" role for organization owners
    op.execute(
        """
        UPDATE organization_member om
        SET role_id = (
            SELECT id FROM role
            WHERE name = 'Owner'
            AND organization_id IS NULL
            LIMIT 1
        )
        FROM organization o
        WHERE om.organization_id = o.id
        AND om.user_id = o.owner_id
        AND om.role_id = (
            SELECT id FROM role
            WHERE name = 'Member'
            AND organization_id IS NULL
            LIMIT 1
        );
        """,
    )

    # Step 5: Validate the constraint
    # This validates existing rows against the constraint without blocking writes
    # New rows are still checked against the constraint even when NOT VALID
    op.execute(
        """
        ALTER TABLE organization_member
        VALIDATE CONSTRAINT fk_organization_member_role_id;
        """,
    )

    # Step 6: Make column NOT NULL
    # Now that all rows have role_id populated, make it required
    # This requires brief ACCESS EXCLUSIVE lock but is quick since data is already set
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
