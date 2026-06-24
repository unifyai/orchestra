"""Allow universal coordinator email contacts.

Revision ID: shared_coordinator_email
Revises: communication_call_sessions
Create Date: 2026-06-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "shared_coordinator_email"
down_revision = "communication_call_sessions"
branch_labels = None
depends_on = None

_OLD_PREDICATE = "status != 'deleted' AND contact_type NOT IN ('whatsapp', 'discord')"
_NEW_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type = 'email' "
    "AND COALESCE(metadata ->> 'universal_unity', 'false') = 'true')"
)


def upgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_NEW_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_OLD_PREDICATE),
    )
