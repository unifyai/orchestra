"""Realign the active-contact uniqueness index to the ``universal_droid`` key.

The shared-Coordinator email/phone routing relies on ``uq_active_contact_value``
*excluding* the universal pool contacts, so every Coordinator can carry the same
shared address/number. That exclusion is keyed on a metadata flag.

Earlier migrations created the index with a ``universal_unity`` flag. The Unity
-> Droid rename later edited those historical migration files in place to say
``universal_droid`` *and* changed the application code to write
``{"universal_droid": true}`` — but environments that had already applied the
original revisions never re-ran them, so their live index still tests
``universal_unity`` while new rows are tagged ``universal_droid``.

The result: the universal pool contacts are no longer excluded from the unique
index, so the *second* Coordinator to receive a shared (e.g. universal email)
contact collides on ``uq_active_contact_value``. This surfaced as a 400 on the
org-wide assistant list (the read-path Coordinator self-heal flush raises a
``UniqueViolation``).

This revision is idempotent w.r.t. the desired end state: it backfills any
lingering ``universal_unity`` flags onto ``universal_droid`` and recreates the
index with the ``universal_droid`` predicate that matches the current model and
application code.

Revision ID: realign_universal_droid_idx
Revises: user_desktop_sftp_tunnel_id
Create Date: 2026-06-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "realign_universal_droid_idx"
down_revision = "user_desktop_sftp_tunnel_id"
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

# Migrate the flag onto whichever key the recreated index will test. The index
# is dropped first so the transient state (rows briefly matching neither the old
# nor the new predicate) cannot raise a uniqueness violation mid-migration.
#
# Match on the flag *value* (not just key presence) so a row that ever stored an
# explicit ``false`` keeps its semantics — only genuine pool contacts are moved.
# Other metadata keys are preserved by the ``-`` (drop one key) + ``||`` (merge)
# pair; no row is deleted and no other field is touched.
_UNIFY_TO_DROID = sa.text(
    "UPDATE assistant_contacts "
    "SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'universal_unity') "
    "|| '{\"universal_droid\": true}'::jsonb "
    "WHERE (metadata ->> 'universal_unity') = 'true'",
)
_DROID_TO_UNITY = sa.text(
    "UPDATE assistant_contacts "
    "SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'universal_droid') "
    "|| '{\"universal_unity\": true}'::jsonb "
    "WHERE (metadata ->> 'universal_droid') = 'true'",
)


def upgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.execute(_UNIFY_TO_DROID)
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_DROID_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.execute(_DROID_TO_UNITY)
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(_UNIFY_PREDICATE),
    )
