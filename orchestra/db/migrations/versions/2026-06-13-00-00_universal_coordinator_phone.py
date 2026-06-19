"""Allow universal coordinator phone contacts.

Revision ID: universal_coordinator_phone
Revises: shared_coordinator_email
Create Date: 2026-06-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "universal_coordinator_phone"
down_revision = "shared_coordinator_email"
branch_labels = None
depends_on = None

_OLD_CONTACT_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type = 'email' "
    "AND COALESCE(metadata ->> 'universal_droid', 'false') = 'true')"
)
_NEW_CONTACT_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type IN ('email', 'phone') "
    "AND COALESCE(metadata ->> 'universal_droid', 'false') = 'true')"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE shared_pool_numbers "
        "DROP CONSTRAINT IF EXISTS shared_pool_numbers_number_key",
    )
    op.create_unique_constraint(
        "uq_shared_pool_number_platform_number",
        "shared_pool_numbers",
        ["platform", "number"],
    )

    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_NEW_CONTACT_PREDICATE),
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_shared_pool_number_platform_number",
        "shared_pool_numbers",
        type_="unique",
    )
    op.create_unique_constraint(
        "shared_pool_numbers_number_key",
        "shared_pool_numbers",
        ["number"],
    )

    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_OLD_CONTACT_PREDICATE),
    )
