"""Allow one coordinator per workspace membership.

Revision ID: workspace_scoped_coordinators
Revises: drop_space_invites
Create Date: 2026-05-21 22:50:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "workspace_scoped_coordinators"
down_revision = "drop_space_invites"
branch_labels = None
depends_on = None

ORG_SINGLETON_INDEX_NAME = "ux_assistants_one_coordinator_per_org"
WORKSPACE_MEMBERSHIP_INDEX_NAME = (
    "ux_assistants_one_workspace_coordinator_per_membership"
)
COORDINATOR_PERSONAL_SCOPE_CHECK_NAME = "ck_assistants_coordinator_personal_scope"


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {ORG_SINGLETON_INDEX_NAME}")
    op.execute(
        "ALTER TABLE assistants "
        f"DROP CONSTRAINT IF EXISTS {COORDINATOR_PERSONAL_SCOPE_CHECK_NAME}",
    )
    op.execute(f"DROP INDEX IF EXISTS {WORKSPACE_MEMBERSHIP_INDEX_NAME}")
    op.create_index(
        WORKSPACE_MEMBERSHIP_INDEX_NAME,
        "assistants",
        ["user_id", "organization_id"],
        unique=True,
        postgresql_where=sa.text("is_coordinator AND organization_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(WORKSPACE_MEMBERSHIP_INDEX_NAME, table_name="assistants")
    op.create_check_constraint(
        COORDINATOR_PERSONAL_SCOPE_CHECK_NAME,
        "assistants",
        "(NOT is_coordinator) OR organization_id IS NULL",
    )
    op.create_index(
        ORG_SINGLETON_INDEX_NAME,
        "assistants",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_coordinator AND organization_id IS NOT NULL"),
    )
