"""Delete the retired All/* aggregation contexts and their rows.

The cross-assistant / cross-user aggregation "view" contexts (topmost
``All/<Sub>`` and intermediate ``<user>/All/<Sub>``) are being removed: nothing
reads them (aggregation is done client-side by fanning out over a context's
owning roots), and they were the only reason assistant/team deletion could not
be a clean owner-scoped drop.

This migration drops those contexts. Their ``ON DELETE CASCADE`` foreign keys
remove the associated ``log_event_context`` / ``field_type`` /
``context_counter`` / ``context_version`` / ``active_derived_log_template``
rows; ``log_unique_constraint`` (whose context FK is not cascade) is cleaned
explicitly. Log events that lived *only* in aggregation contexts (no owning
context) are then purged along with their embeddings and unique-constraint
rows.

Idempotent and a no-op on a fresh/empty database (so CI's migrate-to-head is
unaffected); the data deletion only happens where aggregation contexts exist.

Revision ID: drop_aggregation_contexts
Revises: owner_key_pk
Create Date: 2026-06-25 00:00:00.000000
"""

from alembic import op
from sqlalchemy import text

revision = "drop_aggregation_contexts"
down_revision = "owner_key_pk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Aggregation contexts: a path component exactly equal to "All".
    bind.execute(
        text(
            "CREATE TEMP TABLE _agg_ctx ON COMMIT DROP AS "
            "SELECT id FROM context WHERE name LIKE 'All/%' OR name LIKE '%/All/%'",
        ),
    )
    # Capture the logs referenced into those contexts before the cascade fires.
    bind.execute(
        text(
            "CREATE TEMP TABLE _agg_logs ON COMMIT DROP AS "
            "SELECT DISTINCT log_event_id AS id FROM log_event_context "
            "WHERE context_id IN (SELECT id FROM _agg_ctx)",
        ),
    )

    # log_unique_constraint's context FK is not ON DELETE CASCADE.
    bind.execute(
        text(
            "DELETE FROM log_unique_constraint "
            "WHERE context_id IN (SELECT id FROM _agg_ctx)",
        ),
    )

    # Drop the contexts; cascade removes log_event_context / field_type /
    # context_counter / context_version / active_derived_log_template rows.
    bind.execute(text("DELETE FROM context WHERE id IN (SELECT id FROM _agg_ctx)"))

    # Logs that lived ONLY in aggregation contexts are now orphaned: purge them
    # and their embeddings / unique-constraint rows (the heavy tables have no FK
    # cascade once partitioned).
    bind.execute(
        text(
            "CREATE TEMP TABLE _orphan_logs ON COMMIT DROP AS "
            "SELECT id FROM _agg_logs al WHERE NOT EXISTS "
            "(SELECT 1 FROM log_event_context lec WHERE lec.log_event_id = al.id)",
        ),
    )
    bind.execute(
        text("DELETE FROM embedding WHERE ref_id IN (SELECT id FROM _orphan_logs)"),
    )
    bind.execute(
        text(
            "DELETE FROM log_unique_constraint "
            "WHERE log_event_id IN (SELECT id FROM _orphan_logs)",
        ),
    )
    bind.execute(
        text("DELETE FROM log_event WHERE id IN (SELECT id FROM _orphan_logs)")
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Aggregation contexts were a derived mirror and are not reconstructable.",
    )
