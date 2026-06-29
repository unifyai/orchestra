"""Tune autovacuum/statistics on the kernel partition leaves.

The partition cutover attached ``log_event``/``log_event_context``/``embedding``
as huge DEFAULT partitions. Under Postgres' default autovacuum scale factors
their trigger thresholds sit in the millions of row-changes, so autovacuum and
autoanalyze never fire: planner statistics freeze at the cutover value and the
hot context-log queries degrade into whole-partition sequential scans that
saturate the database. This migration applies fixed, size-independent
autovacuum thresholds (and a higher statistics target on ``log_event.data``) to
every existing kernel leaf, then ANALYZEs them so the planner has fresh stats
immediately. New partitions inherit the same tuning via
``partitioning.tune_partition_storage`` (called from the maintenance worker and
the legacy-conversion path).

Revision ID: tune_kernel_partition_autovacuum
Revises: system_builtins_project
Create Date: 2026-07-04 00:00:00.000000
"""

from alembic import op

from orchestra.db.partitioning import (
    PARTITION_AUTOVACUUM_RELOPTIONS,
    PARTITIONED_TABLES,
    partition_leaves,
    tune_partition_storage,
)

revision = "tune_kernel_partition_autovacuum"
down_revision = "system_builtins_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    tune_partition_storage(conn)
    # Refresh planner stats immediately. ANALYZE is safe inside the migration
    # transaction; VACUUM is left to autovacuum and the maintenance worker.
    for table in PARTITIONED_TABLES:
        for leaf in partition_leaves(conn, table):
            op.execute(f"ANALYZE {leaf}")


def downgrade() -> None:
    conn = op.get_bind()
    keys = ", ".join(PARTITION_AUTOVACUUM_RELOPTIONS)
    for table in PARTITIONED_TABLES:
        for leaf in partition_leaves(conn, table):
            op.execute(f"ALTER TABLE {leaf} RESET ({keys})")
    for leaf in partition_leaves(conn, "log_event"):
        op.execute(f"ALTER TABLE {leaf} ALTER COLUMN data SET STATISTICS -1")
