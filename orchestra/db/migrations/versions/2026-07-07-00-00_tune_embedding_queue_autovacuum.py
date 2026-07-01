"""Right-size autovacuum for the high-churn embedding_queue leaves.

``embedding_queue`` is a work queue (insert -> claim -> delete), not a growing
store, so it should sit near-empty. The earlier kernel tuning applied the same
fixed 50k thresholds to every partitioned leaf, which is correct for the
multi-million-row log/embedding partitions but far too high for the queue: it
accumulates tens of thousands of dead tuples (observed ~47k dead against ~1k
live) before autovacuum fires. This re-runs the now queue-aware
``tune_partition_storage`` so ``embedding_queue`` leaves get small thresholds,
then ANALYZEs the kernel leaves so stats are fresh immediately.

A one-off ``VACUUM (ANALYZE) embedding_queue_default`` on prod after deploy
reclaims the currently-accumulated dead tuples (VACUUM cannot run inside this
migration's transaction).

Revision ID: tune_embedding_queue_autovacuum
Revises: disable_personal_workspaces
Create Date: 2026-07-07 00:00:00.000000
"""

from alembic import op

from orchestra.db.partitioning import (
    PARTITION_AUTOVACUUM_RELOPTIONS,
    PARTITIONED_TABLES,
    QUEUE_AUTOVACUUM_RELOPTIONS,
    partition_leaves,
    tune_partition_storage,
)

revision = "tune_embedding_queue_autovacuum"
down_revision = "disable_personal_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    tune_partition_storage(conn)
    # Refresh planner stats immediately. ANALYZE is safe inside the migration
    # transaction; VACUUM is left to autovacuum and the one-off prod cleanup.
    for table in PARTITIONED_TABLES:
        for leaf in partition_leaves(conn, table):
            op.execute(f"ANALYZE {leaf}")


def downgrade() -> None:
    conn = op.get_bind()
    # Restore the shared large-partition tuning on the queue leaves.
    keys = ", ".join(QUEUE_AUTOVACUUM_RELOPTIONS)
    reloptions = ", ".join(
        f"{k} = {v}" for k, v in PARTITION_AUTOVACUUM_RELOPTIONS.items()
    )
    for leaf in partition_leaves(conn, "embedding_queue"):
        op.execute(f"ALTER TABLE {leaf} RESET ({keys})")
        op.execute(f"ALTER TABLE {leaf} SET ({reloptions})")
