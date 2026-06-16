"""Make assistant contact value uniqueness predicate NULL-safe.

Revision ID: fix_contact_value_index_predicate
Revises: referral_program
Create Date: 2026-06-19 04:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "fix_contact_value_index_predicate"
down_revision = "referral_program"
branch_labels = None
depends_on = None

_PREVIOUS_CONTACT_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type IN ('email', 'phone') "
    "AND (metadata ->> 'universal_unity') = 'true')"
)
_CONTACT_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type IN ('email', 'phone') "
    "AND COALESCE(metadata ->> 'universal_unity', 'false') = 'true')"
)


def upgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_CONTACT_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_PREVIOUS_CONTACT_PREDICATE),
    )
