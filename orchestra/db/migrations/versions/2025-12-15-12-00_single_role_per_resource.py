"""Enforce single role per user/team per resource

This migration changes the ResourceAccess constraint to allow only one role
per grantee (user or team) per resource. Previously, a user could have multiple
different roles on the same resource.

Changes:
1. Removes duplicate grants (keeps most recent by created_at)
2. Drops old constraint that included role_id
3. Creates new constraint without role_id

Revision ID: single_role_per_resource
Revises: remove_level_column
Create Date: 2025-12-15 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "single_role_per_resource"
down_revision = "7f6be7eb120c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Enforce single role per user/team per resource.

    1. Remove duplicate grants, keeping the most recent one (by created_at)
    2. Drop old unique constraint (includes role_id)
    3. Create new unique constraint (excludes role_id)
    """
    # Step 1: Remove duplicates, keeping the most recent grant for each
    # (resource_type, resource_id, grantee_type, grantee_id) combination.
    # This uses a subquery to find records where another record exists with
    # the same grantee/resource but a newer created_at timestamp.
    op.execute(
        """
        DELETE FROM resource_access ra1
        WHERE EXISTS (
            SELECT 1 FROM resource_access ra2
            WHERE ra1.resource_type = ra2.resource_type
              AND ra1.resource_id = ra2.resource_id
              AND ra1.grantee_type = ra2.grantee_type
              AND ra1.grantee_id = ra2.grantee_id
              AND ra1.id != ra2.id
              AND (
                  ra1.created_at < ra2.created_at
                  OR (ra1.created_at = ra2.created_at AND ra1.id < ra2.id)
              )
        );
        """,
    )

    # Step 2: Drop old unique constraint (includes role_id)
    op.drop_constraint("uq_resource_access", "resource_access", type_="unique")

    # Step 3: Create new unique constraint (excludes role_id)
    # This ensures only one grant per grantee per resource
    op.create_unique_constraint(
        "uq_resource_access_grantee",
        "resource_access",
        ["resource_type", "resource_id", "grantee_type", "grantee_id"],
    )


def downgrade() -> None:
    """
    Revert to allowing multiple roles per user/team per resource.
    """
    # Drop new constraint
    op.drop_constraint("uq_resource_access_grantee", "resource_access", type_="unique")

    # Restore old constraint (includes role_id)
    op.create_unique_constraint(
        "uq_resource_access",
        "resource_access",
        ["resource_type", "resource_id", "role_id", "grantee_type", "grantee_id"],
    )
