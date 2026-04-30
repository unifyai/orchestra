"""Add Coordinator role and organization-default space kind.

Coordinators are assistants with a role-defining boolean flag. Space kind
distinguishes regular team spaces from the organization-default space.

Revision ID: add_coordinator_columns
Revises: add_spaces
Create Date: 2026-04-30 13:20:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_coordinator_columns"
down_revision = "add_spaces"
branch_labels = None
depends_on = None

PERSONAL_COORDINATOR_INDEX_NAME = "ux_assistants_one_personal_coordinator_per_user"
ORG_DEFAULT_SPACE_INDEX_NAME = "ux_spaces_one_org_default_per_org"


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "is_coordinator",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "spaces",
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'team'"),
        ),
    )
    op.create_check_constraint(
        "ck_spaces_kind",
        "spaces",
        "kind IN ('team', 'org_default')",
    )
    op.create_index(
        PERSONAL_COORDINATOR_INDEX_NAME,
        "assistants",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_coordinator AND organization_id IS NULL",
        ),
    )
    op.create_index(
        ORG_DEFAULT_SPACE_INDEX_NAME,
        "spaces",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'org_default'"),
    )


def downgrade() -> None:
    op.drop_index(
        ORG_DEFAULT_SPACE_INDEX_NAME,
        table_name="spaces",
    )
    op.drop_index(
        PERSONAL_COORDINATOR_INDEX_NAME,
        table_name="assistants",
    )
    op.drop_constraint("ck_spaces_kind", "spaces", type_="check")
    op.drop_column("spaces", "kind")
    op.drop_column("assistants", "is_coordinator")
