"""Index assistant_secrets(secret_name, agent_id) for provider-scoped listing.

The admin assistant list can now filter to assistants that have a given provider
secret (``require_secret_names``, e.g. ``MICROSOFT_REFRESH_TOKEN``) so the
token-refresh cron does not enumerate every assistant. The table PK is
``(agent_id, secret_name)``, whose leading column is ``agent_id`` and therefore
cannot serve a ``secret_name``-first lookup. Add a ``(secret_name, agent_id)``
index so that filter stays cheap as the table grows.

Revision ID: idx_asst_secrets_secret_name
Revises: coord_default_voice_backfill
Create Date: 2026-07-09 00:00:00.000000
"""

from alembic import op

revision = "idx_asst_secrets_secret_name"
down_revision = "coord_default_voice_backfill"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_assistant_secrets_secret_name_agent_id"


def upgrade() -> None:
    # IF NOT EXISTS keeps the migration safe to re-run after a partial failure.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        "ON assistant_secrets (secret_name, agent_id)",
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
