"""Team-owned assistants: owner_team_id on the assistants table.

Revision ID: assistant_owner_team
Revises: presence_and_org_chat
Create Date: 2026-07-15 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "assistant_owner_team"
down_revision = "presence_and_org_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("owner_team_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_assistants_owner_team_id",
        "assistants",
        "team",
        ["owner_team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_assistants_owner_team_id",
        "assistants",
        ["owner_team_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistants_owner_team_id", table_name="assistants")
    op.drop_constraint(
        "fk_assistants_owner_team_id",
        "assistants",
        type_="foreignkey",
    )
    op.drop_column("assistants", "owner_team_id")
