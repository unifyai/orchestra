"""Track disabled personal workspaces for organization members.

Revision ID: disable_personal_workspaces
Revises: reproject_child_partition_key
Create Date: 2026-07-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# Kept <= 32 chars: alembic_version.version_num is varchar(32).
revision = "disable_personal_workspaces"
down_revision = "reproject_child_partition_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "personal_workspace_disabled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "user",
        sa.Column("personal_workspace_disabled_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("personal_workspace_disabled_org_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_personal_workspace_disabled_org_id",
        "user",
        "organization",
        ["personal_workspace_disabled_org_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_user_personal_workspace_disabled_org_id",
        "user",
        ["personal_workspace_disabled_org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_personal_workspace_disabled_org_id", table_name="user")
    op.drop_constraint(
        "fk_user_personal_workspace_disabled_org_id",
        "user",
        type_="foreignkey",
    )
    op.drop_column("user", "personal_workspace_disabled_org_id")
    op.drop_column("user", "personal_workspace_disabled_reason")
    op.drop_column("user", "personal_workspace_disabled_at")
