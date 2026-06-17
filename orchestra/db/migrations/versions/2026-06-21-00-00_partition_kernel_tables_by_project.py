"""Partition kernel log/embedding tables by project_id.

Provisions the child partitions for the LIST(project_id)-partitioned kernel
tables (``log_event``, ``log_event_context``, ``embedding``,
``embedding_queue``). Deleting a project becomes an O(1) ``DROP PARTITION``
instead of per-row GIN/HNSW index churn.

Two starting states are handled:

* **Fresh database** -- ``0001_core_initial`` runs ``meta.create_all`` against
  the current models, which now carry ``postgresql_partition_by`` and therefore
  build these tables as partitioned *parents*. A partitioned parent has no
  storage until a partition is attached, so this migration creates the DEFAULT
  partition (plus a dedicated partition for any known giant project that
  already exists in this environment). After this, inserts succeed.

* **Existing populated database** -- the live ``log_event`` (~72 GB) and
  ``embedding`` (~96 GB) tables are still ordinary (non-partitioned) relations.
  Converting them cannot be an in-place ``ALTER`` and a multi-hour batched
  backfill must not run inside an alembic transaction. This migration therefore
  *refuses* to touch a populated non-partitioned table and directs the operator
  to ``scripts/partition_backfill_cutover.py``, which performs the online
  create-backfill-swap and then ``alembic stamp``s this revision.

Revision ID: partition_kernel_by_project
Revises: assistant_workspace_file_access
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op

from orchestra.db.partitioning import (
    PARTITIONED_TABLES,
    child_partitions,
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

# Projects large enough to warrant their own partition (prod-measured:
# 189888 ~18M rows, 121850 ~3.7M, 130937 ~415K). On staging/CI these ids do not
# exist and are skipped (their rows fall into the DEFAULT partition); a project
# is promoted to a dedicated partition later by the index-maintenance worker
# once it crosses a size threshold.
GIANT_PROJECT_IDS: tuple[int, ...] = (189888, 121850, 130937)

_RUNBOOK_HINT = (
    "Table %r is a populated non-partitioned relation. Converting it in place "
    "is not safe inside a migration. Run the online create-backfill-swap "
    "runbook instead:\n\n"
    "    python -m scripts.partition_backfill_cutover --table %s create backfill index cutover\n\n"
    "The runbook stamps this revision (partition_kernel_by_project) on the "
    "alembic_version table when the cutover completes."
)


def _ensure_table_partitioned(bind, table: str) -> None:
    """Make ``table`` a partitioned parent that has its DEFAULT (+giant) partitions.

    Fresh deploys reach here with the parent already partitioned (relkind 'p');
    we only need to attach partitions. An empty legacy table is recreated from
    the (partitioned) model. A populated legacy table is left untouched and the
    operator is pointed at the runbook.
    """
    kind = table_relkind(bind, table)

    if kind == "p":
        giants = existing_project_ids(bind, GIANT_PROJECT_IDS)
        ensure_partitions(bind, table, giants)
        return

    if kind == "r":
        if table_has_rows(bind, table):
            raise RuntimeError(_RUNBOOK_HINT % (table, table))
        # Empty legacy table: recreate from the (now partitioned) model, then
        # attach partitions. Safe because increment 1 dropped every inter-table
        # FK that pointed at log_event.
        op.drop_table(table)
        kind = None

    if kind is None:
        from orchestra.db.meta import meta
        from orchestra.db.models import load_all_models

        load_all_models()
        meta.tables[table].create(bind=bind)
        ensure_partitions(bind, table, ())


def upgrade() -> None:
    bind = op.get_bind()
    for table in PARTITIONED_TABLES:
        _ensure_table_partitioned(bind, table)


def downgrade() -> None:
    """Detach and drop the child partitions, leaving bare partitioned parents.

    This is the inverse of the fresh-deploy path. A production database that was
    converted via the runbook is rolled back by renaming the preserved ``*_old``
    tables back into place (see the runbook's ``rollback`` phase), not here.
    """
    bind = op.get_bind()
    for table in PARTITIONED_TABLES:
        if table_relkind(bind, table) != "p":
            continue
        for child in child_partitions(bind, table):
            op.execute(f'ALTER TABLE "{table}" DETACH PARTITION "{child}"')
            op.execute(f'DROP TABLE IF EXISTS "{child}"')
        # Keep the named DEFAULT partition reference clean for re-upgrade.
        op.execute(f'DROP TABLE IF EXISTS "{default_partition_name(table)}"')
