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

from alembic import op

from orchestra.db.scope import reclassify_heavy_owner_keys

revision = "reclassify_owner_keys"
down_revision = "owner_delete_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        reclassify_heavy_owner_keys(op.get_bind())


def downgrade() -> None:
    # Pure data correction; the prior ``'sys'`` values cannot be reconstructed
    # (they were indistinguishable from genuine system rows), so there is nothing
    # safe to reverse.
    pass
