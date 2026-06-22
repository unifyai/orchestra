"""Single-column ``id`` index on the partitioned ``log_event`` table.

``log_event``'s primary key is composite ``(project_id, id, owner_key)`` and
leads with ``project_id``, so a lookup by bare ``id`` (point reads that resolve
a log's owning project/owner before ``project_id`` is known -- ``get_ts``,
``get_user_id``, ``get_user_and_project_id``, ``owner_key_for_log``) has no
usable index and degrades to a scan of every partition. This adds a plain
``(id)`` btree so those probes are indexed across the partition tree.

The build is lock-safe (``ON ONLY`` parent + ``CREATE INDEX CONCURRENTLY`` per
leaf + ``ATTACH``; see :func:`orchestra.db.partitioning.build_partitioned_index`)
and idempotent: on a fresh deploy the index already exists (it is in the model
``__table_args__``, so each ``CREATE TABLE ... PARTITION OF`` auto-attaches a
child copy), and already-attached children are detected and skipped.

``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction, so the build runs
in an ``autocommit_block`` (the migrator's session ``lock_timeout`` from
``env.py`` still applies).

Revision ID: log_event_id_index
Revises: org_wide_sharing
Create Date: 2026-06-28 00:00:00.000000
"""

from alembic import op
from sqlalchemy import text

from orchestra.db.partitioning import (
    build_partitioned_index,
    is_partitioned,
    relation_exists,
)

revision = "log_event_id_index"
down_revision = "org_wide_sharing"
branch_labels = None
depends_on = None

_TABLE = "log_event"
_INDEX_NAME = "idx_log_event_id"
_COLS = '"id"'
_LEAF_SUFFIX = "id_idx"


def upgrade() -> None:
    bind = op.get_bind()
    if not relation_exists(bind, _TABLE):
        return
    # CREATE INDEX CONCURRENTLY forbids an open transaction; the session
    # lock_timeout set in env.py persists across the autocommit toggle.
    with op.get_context().autocommit_block():
        if is_partitioned(bind, _TABLE):
            build_partitioned_index(
                bind,
                _TABLE,
                _INDEX_NAME,
                _COLS,
                leaf_suffix=_LEAF_SUFFIX,
                concurrently=True,
            )
        else:
            bind.execute(
                text(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{_INDEX_NAME}" '
                    f'ON "{_TABLE}" ({_COLS})',
                ),
            )


def downgrade() -> None:
    # Dropping the parent partitioned index cascades to every attached leaf.
    op.execute(f'DROP INDEX IF EXISTS "{_INDEX_NAME}"')
