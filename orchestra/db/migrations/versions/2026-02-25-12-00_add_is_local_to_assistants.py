"""Add is_local column to assistants table

Local assistants use production orchestra/adapters/communication but run
unity locally instead of on GKE.  The flag replaces the brittle
"default assistant" heuristics (string checks and ID < 10 guards) that
were previously scattered across the adapters codebase.

Revision ID: add_is_local
Revises: add_user_desktops
Create Date: 2026-02-25 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "add_is_local"
down_revision = "add_user_desktops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "is_local",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "is_local")
