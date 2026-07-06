"""Add non-unique lookup index on assistant_contacts (type, value).

Supports admin contact-filter queries that resolve assistants by
``contact_type`` + ``contact_value``. Unlike ``uq_active_contact_value``,
this index includes universal Coordinator pool contacts (many assistants
share the same address) so reverse lookups stay index-backed.

Revision ID: idx_assistant_contacts_lookup
Revises: idx_asst_secrets_secret_name
Create Date: 2026-07-10 00:00:00.000000
"""

from alembic import op

revision = "idx_assistant_contacts_lookup"
down_revision = "idx_asst_secrets_secret_name"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_assistant_contacts_lookup"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        "ON assistant_contacts (contact_type, contact_value) "
        "WHERE status != 'deleted'",
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
