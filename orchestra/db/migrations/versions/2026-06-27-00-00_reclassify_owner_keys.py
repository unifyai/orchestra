"""Corrective backfill: reclassify heavy rows mislabelled ``'sys'`` to their owner.

The ``heavy_owner_key`` migration added ``owner_key`` with ``DEFAULT 'sys'`` (to
survive the live ``NOT NULL`` promotion against old-app writes), which set every
pre-existing row to ``'sys'`` before ``backfill_heavy_owner_keys`` ran -- and
that backfill only updates ``owner_key IS NULL`` rows, so it was a no-op for all
historical data. The result: an assistant/team's pre-existing ``log_event`` /
``log_event_context`` / ``embedding`` rows stayed ``'sys'`` even though their
owning context identifies the real owner, so owner-scoped deletion
(``purge_owner`` / ``drop_owner``) would leave them orphaned.

This recomputes ``owner_key`` from each log's owning (assistant/team) context for
rows still labelled ``'sys'`` (see
:func:`orchestra.db.scope.reclassify_heavy_owner_keys`). Genuinely system-owned
rows (no assistant/team owning context) stay ``'sys'``.

Runs in an ``autocommit_block`` so each batched UPDATE commits independently --
the correction touches real data volume, and a single transaction over a large
``log_event`` would hold locks / bloat WAL for too long. Idempotent and
resumable; the migrator ``lock_timeout`` from ``env.py`` still applies.

Revision ID: reclassify_owner_keys
Revises: owner_delete_index
Create Date: 2026-06-27 00:00:00.000000
"""

revision = "reclassify_owner_keys"
down_revision = "owner_delete_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentionally a no-op.
    #
    # This correction was originally run inline here, but the full-table,
    # three-pass UPDATE was far too slow at production scale (it scanned the
    # whole ~60M-row heavy family and rewrote a primary-key column, bloating the
    # tables) and could not finish inside the migrator's timeout -- while also
    # blocking the deploy. A data correction of that size does not belong in the
    # synchronous, time-bounded deploy migrate step.
    #
    # The reclassification now runs **out-of-band** as a standalone, resumable
    # maintenance job that is driven from the (small) set of assistant/team
    # contexts and is proportional to the mislabelled data:
    # ``scripts/reclassify_owner_keys.py`` -> ``scope.reclassify_heavy_owner_keys``.
    # This revision is kept (already stamped on some environments) so the alembic
    # chain stays linear; it simply advances the version with no schema/data work.
    pass


def downgrade() -> None:
    # No-op upgrade -> nothing to reverse.
    pass
