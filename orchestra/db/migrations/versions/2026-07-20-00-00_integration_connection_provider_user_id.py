"""Add provider_user_id to integration_connections.

Provider backends (Composio) bind each connected account to an entity id at
link time and require the same id on every execution. Deriving it from the
caller's owner scope breaks whenever the connection's owner context differs
from the linking context (assistant ownership transfers, cross-environment
mirroring), so the linked entity id is persisted on the connection row.

Revision ID: integration_conn_provider_uid
Revises: ms_teams_bot_active_owner_idx
"""

import sqlalchemy as sa
from alembic import op

revision = "integration_conn_provider_uid"
down_revision = "ms_teams_bot_active_owner_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_connections",
        sa.Column("provider_user_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_connections", "provider_user_id")
