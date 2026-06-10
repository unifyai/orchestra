"""Drop retired deploy_env columns.

Per-assistant environment routing was removed; every deployment is
env-pinned and resolves its environment from process configuration
(see ``orchestra.lib.deploy_env``).

Revision ID: drop_deploy_env_columns
Revises: universal_coordinator_phone
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_deploy_env_columns"
down_revision = "universal_coordinator_phone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("assistants", "deploy_env")
    op.drop_column("assistant_cleanup_tasks", "deploy_env")


def downgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("deploy_env", sa.String(), nullable=True),
    )
    op.add_column(
        "assistant_cleanup_tasks",
        sa.Column("deploy_env", sa.String(), nullable=True),
    )
