"""Partition kernel log/embedding tables by project_id.

Partitions the heavy kernel tables (``log_event``, ``log_event_context``,
``embedding``, ``embedding_queue``) ``BY LIST (project_id)`` so deleting a
project/org/user becomes an O(1) ``DROP PARTITION`` instead of per-row GIN/HNSW
index churn, and maintenance runs per-partition.

Two starting states are handled automatically:

* **Fresh database** -- ``0001_core_initial`` runs ``meta.create_all`` against
  the current models, which carry ``postgresql_partition_by`` and therefore
  build these tables as partitioned *parents* with no storage. This migration
  just creates the DEFAULT partition for each.

* **Existing populated database** (staging/prod) -- the tables are still
  ordinary (non-partitioned) relations. This migration converts them *in place*
  via :func:`convert_legacy_to_partitioned`: each table is reshaped to the
  partitioned schema (denormalize + NOT NULL ``project_id`` on child tables,
  drop the FKs that referenced ``log_event``, swap to the composite PK) and then
  attached as the ``DEFAULT`` partition of a freshly created partitioned parent.
  No bulk data copy and no index rebuild -- the existing GIN/HNSW indexes ride
  along with the attached partition. This takes brief exclusive locks (run in a
  maintenance window), but it is a normal in-transaction migration that the CI
  migrate-then-deploy pipeline runs.

Giant projects are NOT carved into dedicated partitions here; they are promoted
later (see the index-maintenance worker's ``promote`` mode), which is where the
per-project O(1) ``DROP PARTITION`` deletion win is realized.

Revision ID: partition_kernel_by_project
Revises: assistant_workspace_file_access
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op

from orchestra.db.partitioning import (
    PARTITIONED_TABLES,
    child_partitions,
    convert_legacy_to_partitioned,
    default_partition_name,
    ensure_partitions,
    existing_project_ids,
    table_has_rows,
    table_relkind,
)

revision = "partition_kernel_by_project"
down_revision = "assistant_workspace_file_access"
branch_labels = None
depends_on = None

# Known giant projects (prod-measured). A dedicated partition is created here
# only if the project already exists in this environment; otherwise its rows
# live in DEFAULT until the maintenance worker promotes it.
GIANT_PROJECT_IDS: tuple[int, ...] = (189888, 121850, 130937)


def upgrade() -> None:
    bind = op.get_bind()
    kind = table_relkind(bind, "log_event")

    if kind == "p":
        # Fresh deploy: parents already partitioned (no storage). Attach the
        # DEFAULT (+ any existing giant) partition to each.
        giants = existing_project_ids(bind, GIANT_PROJECT_IDS)
        for table in PARTITIONED_TABLES:
            ensure_partitions(bind, table, giants)
        return

    if kind == "r":
        # Existing populated (or empty) legacy tables: convert in place.
        convert_legacy_to_partitioned(bind)
        return

    raise RuntimeError(
        "log_event relation not found; cannot apply partitioning migration.",
    )


def downgrade() -> None:
    """Detach and drop *empty* child partitions, leaving bare partitioned parents.

    This reverses the fresh-deploy path. It deliberately refuses to drop a
    non-empty partition: the populated in-place conversion is not auto-reversible
    (it dropped FKs and rewrote PKs), so a production rollback must restore from
    backup rather than silently destroy data here.
    """
    bind = op.get_bind()
    for table in PARTITIONED_TABLES:
        if table_relkind(bind, table) != "p":
            continue
        for child in child_partitions(bind, table):
            if table_has_rows(bind, child):
                raise RuntimeError(
                    f"Refusing to downgrade: partition {child!r} is non-empty. "
                    "Restore from backup to reverse the populated conversion.",
                )
            op.execute(f'ALTER TABLE "{table}" DETACH PARTITION "{child}"')
            op.execute(f'DROP TABLE IF EXISTS "{child}"')
        op.execute(f'DROP TABLE IF EXISTS "{default_partition_name(table)}"')
