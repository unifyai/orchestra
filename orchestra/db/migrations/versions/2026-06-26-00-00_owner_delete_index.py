"""Owner-scoped deletion index on the heavy partitioned tables.

Adds a btree ``(project_id, owner_key)`` index to ``log_event`` and
``log_event_context`` so an owner-scoped purge (``DELETE ... WHERE project_id
AND owner_key``, the per-assistant / per-team deletion path) is proportional to
that owner's rows instead of scanning the whole shared Assistants project.
``embedding`` is already covered by ``uq_embedding``'s ``(project_id,
owner_key, ...)`` prefix.

The build is lock-safe (``ON ONLY`` parent + ``CREATE INDEX CONCURRENTLY`` per
leaf + ``ATTACH``; see :func:`orchestra.db.partitioning.build_partitioned_index`)
and idempotent: on a fresh deploy the index already exists (it is in the model
``__table_args__``, so each ``CREATE TABLE ... PARTITION OF`` auto-attaches a
child copy), and already-attached children are detected and skipped.

``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction, so the build runs
in an ``autocommit_block`` (the migrator's session ``lock_timeout`` from
``env.py`` still applies).

Revision ID: owner_delete_index
Revises: drop_aggregation_contexts
Create Date: 2026-06-26 00:00:00.000000
"""

from alembic import op
from sqlalchemy import text

from orchestra.db.partitioning import (
    build_partitioned_index,
    is_partitioned,
    relation_exists,
)

revision = "owner_delete_index"
down_revision = "drop_aggregation_contexts"
branch_labels = None
depends_on = None

# Parent table -> canonical (model-declared) index name. Both tables carry
# ``project_id`` and ``owner_key``, so the index column list is shared.
_INDEXES: dict[str, str] = {
    "log_event": "idx_log_event_project_owner",
    "log_event_context": "idx_log_event_context_project_owner",
}
_COLS = '"project_id", "owner_key"'
_LEAF_SUFFIX = "powner_idx"


def upgrade() -> None:
    bind = op.get_bind()
    # CREATE INDEX CONCURRENTLY forbids an open transaction; the session
    # lock_timeout set in env.py persists across the autocommit toggle.
    with op.get_context().autocommit_block():
        for table, index_name in _INDEXES.items():
            if not relation_exists(bind, table):
                continue
            if is_partitioned(bind, table):
                build_partitioned_index(
                    bind,
                    table,
                    index_name,
                    _COLS,
                    leaf_suffix=_LEAF_SUFFIX,
                    concurrently=True,
                )
            else:
                bind.execute(
                    text(
                        f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                        f'ON "{table}" ({_COLS})',
                    ),
                )


def downgrade() -> None:
    # Dropping the parent partitioned index cascades to every attached leaf.
    for index_name in _INDEXES.values():
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
