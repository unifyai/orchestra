"""Realign the active-contact uniqueness index back to the ``universal_unity`` key.

Part of the Droid -> Unity consolidation. The earlier
``realign_universal_droid_idx`` revision moved the shared-Coordinator pool flag
onto ``universal_droid`` and recreated ``uq_active_contact_value`` with the
``universal_droid`` predicate. The consolidation reverts the application code to
write ``{"universal_unity": true}``, so this revision realigns the live index to
match: it backfills any ``universal_droid`` flags onto ``universal_unity`` and
recreates the index with the ``universal_unity`` predicate.

This preserves the original fix's intent (universal pool contacts excluded from
``uq_active_contact_value`` so every Coordinator can carry the same shared
address/number) -- only the metadata key changes back to ``universal_unity``.

The index is dropped first so the transient state (rows briefly matching neither
predicate) cannot raise a uniqueness violation mid-migration. The flag move uses
``-`` (drop one key) + ``||`` (merge) so no other metadata field is touched and
no row is deleted.

Revision ID: realign_universal_unity_idx
Revises: realign_universal_droid_idx
Create Date: 2026-07-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "realign_universal_unity_idx"
down_revision = "realign_universal_droid_idx"
branch_labels = None
depends_on = None

_DROID_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type IN ('email', 'phone') "
    "AND COALESCE(metadata ->> 'universal_droid', 'false') = 'true')"
)
_UNIFY_PREDICATE = (
    "status != 'deleted' "
    "AND contact_type NOT IN ('whatsapp', 'discord') "
    "AND NOT (contact_type IN ('email', 'phone') "
    "AND COALESCE(metadata ->> 'universal_unity', 'false') = 'true')"
)

_DROID_TO_UNITY = sa.text(
    "UPDATE assistant_contacts "
    "SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'universal_droid') "
    "|| '{\"universal_unity\": true}'::jsonb "
    "WHERE (metadata ->> 'universal_droid') = 'true'",
)
_UNIFY_TO_DROID = sa.text(
    "UPDATE assistant_contacts "
    "SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'universal_unity') "
    "|| '{\"universal_droid\": true}'::jsonb "
    "WHERE (metadata ->> 'universal_unity') = 'true'",
)


def upgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.execute(_DROID_TO_UNITY)
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_UNIFY_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.execute(_UNIFY_TO_DROID)
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_DROID_PREDICATE),
    )
