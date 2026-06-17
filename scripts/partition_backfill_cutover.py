#!/usr/bin/env python3
"""Online create-backfill-swap migration for the LIST(project_id) kernel tables.

The live ``log_event`` (~72 GB) and ``embedding`` (~96 GB) tables cannot be
converted to partitioned form with an in-place ``ALTER``, and a multi-hour
backfill must not run inside an alembic transaction. This runbook performs the
conversion online, in resumable phases, with only the final ``cutover`` phase
needing a brief lock (run it in a maintenance window).

Per table the new partitioned shadow is ``<table>_p`` and the preserved
pre-cutover original is ``<table>_old`` (kept for rollback until verified).

Phases (run one or more, in order):

  create    Build ``<table>_p`` partitioned BY LIST (project_id) with only its
            inline constraints (PK, unique, check, FK) and NO secondary indexes,
            so the backfill writes at full speed. Create the DEFAULT partition
            plus a dedicated partition per ``--giant-project``.
  backfill  Batched, idempotent (ON CONFLICT DO NOTHING) copy of rows from the
            original table into ``<table>_p``, deriving the denormalized
            ``project_id`` from ``log_event`` for the child tables. Online; no
            exclusive lock. Resumable -- just re-run.
  index     Build every secondary index on ``<table>_p`` -- the plain btree
            indexes plus the expensive GIN (``log_event.data``) and partial
            HNSW (``embedding``) indexes. This is the slow offline step; run it
            before the maintenance window.
  cutover   MAINTENANCE WINDOW. Lock the original, catch up the delta, rename
            original -> ``<table>_old`` and ``<table>_p`` -> original, reset the
            id sequences, and (unless --no-stamp) stamp the alembic revision.
  verify    Compare row counts, show a partition-pruning EXPLAIN, and sample an
            ANN query (embedding only).
  rollback  Rename original -> ``<table>_failed`` and ``<table>_old`` -> original.
  drop-old  DROP ``<table>_old`` (only after the new schema is verified good).

Examples
--------
    # Staging dry run of the whole pipeline for log_event
    python -m scripts.partition_backfill_cutover \
        --table log_event --giant-project 189888 --dry-run \
        create backfill index

    # Real run, all tables, prod giants, via an explicit proxy URL
    python -m scripts.partition_backfill_cutover \
        --db-url 'postgresql+psycopg2://...@127.0.0.1:5432/orchestra' \
        --giant-project 189888 --giant-project 121850 --giant-project 130937 \
        create backfill index

    # In the maintenance window
    python -m scripts.partition_backfill_cutover cutover verify
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from orchestra.db.partitioning import (
    PARTITIONED_TABLES,
    child_partitions,
    create_dedicated_partition,
    create_default_partition,
    dedicated_partition_name,
    table_relkind,
)
from orchestra.settings import settings

# Index/constraint names are schema-global, so while the legacy table still
# holds the canonical names the shadow's explicitly-named indexes and unique
# constraints carry this suffix. The `finalize` phase renames them back to the
# canonical names once the legacy table has been dropped.
SHADOW_NAME_SUFFIX = "__shadow"

# How the denormalized project_id is sourced for each child table during
# backfill, plus the column list and a stable ordering/watermark column.
#   project_expr : SQL expression yielding project_id for the shadow row
#   from_clause  : original table (+ join to log_event when denormalizing)
#   columns      : (shadow_columns, select_exprs) kept positionally aligned
#   watermark    : column used to page through the source in id order
_TABLE_PLANS: dict[str, dict] = {
    "log_event": {
        "from_clause": "log_event src",
        "columns": [
            ("project_id", "src.project_id"),
            ("id", "src.id"),
            ("data", "src.data"),
            ("key_order", "src.key_order"),
            ("created_at", "src.created_at"),
            ("updated_at", "src.updated_at"),
        ],
        "watermark": "src.id",
        "where": None,
    },
    "log_event_context": {
        "from_clause": (
            "log_event_context src " "JOIN log_event le ON le.id = src.log_event_id"
        ),
        "columns": [
            ("project_id", "le.project_id"),
            ("log_event_id", "src.log_event_id"),
            ("context_id", "src.context_id"),
        ],
        "watermark": "src.log_event_id",
        "where": None,
    },
    "embedding": {
        "from_clause": "embedding src JOIN log_event le ON le.id = src.ref_id",
        "columns": [
            ("project_id", "le.project_id"),
            ("id", "src.id"),
            ("ref_id", "src.ref_id"),
            ("model", "src.model"),
            ("key", "src.key"),
            ("vector", "src.vector"),
            ("created_at", "src.created_at"),
            ("is_deleted", "src.is_deleted"),
        ],
        "watermark": "src.id",
        # ref_id IS NULL embeddings are legacy orphans (SET NULL on a deleted
        # log_event); they have no project and are intentionally not migrated.
        "where": "src.ref_id IS NOT NULL",
    },
    "embedding_queue": {
        "from_clause": "embedding_queue src JOIN log_event le ON le.id = src.ref_id",
        "columns": [
            ("project_id", "le.project_id"),
            ("id", "src.id"),
            ("ref_id", "src.ref_id"),
            ("key", "src.key"),
            ("text", "src.text"),
            ("model", "src.model"),
            ("dimensions", "src.dimensions"),
            ("status", "src.status"),
            ("retry_count", "src.retry_count"),
            ("error_message", "src.error_message"),
            ("created_at", "src.created_at"),
            ("processing_started_at", "src.processing_started_at"),
            ("generated_vector", "src.generated_vector"),
            ("vector_generated_at", "src.vector_generated_at"),
        ],
        "watermark": "src.id",
        "where": None,
    },
}


def _shadow(table: str) -> str:
    return f"{table}_p"


def _old(table: str) -> str:
    return f"{table}_old"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _exec(conn: Connection, sql: str, dry_run: bool, params: dict | None = None):
    if dry_run:
        _log(f"DRY-RUN SQL:\n    {sql.strip()}")
        return None
    return conn.execute(text(sql), params or {})


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
@contextmanager
def _shadow_clone(table: str):
    """Yield a Table cloned from the model under the ``<table>_p`` name.

    Cloned into the *live* metadata (not a fresh one) so FK targets such as
    ``project`` and ``context`` resolve at compile time. Explicitly-named
    indexes and unique constraints are suffixed (:data:`SHADOW_NAME_SUFFIX`) to
    avoid colliding with the still-present legacy table's schema-global index
    names. Detached from the metadata on exit.
    """
    from orchestra.db.meta import meta
    from orchestra.db.models import load_all_models

    load_all_models()
    name = _shadow(table)
    if name in meta.tables:
        meta.remove(meta.tables[name])
    clone = meta.tables[table].to_metadata(meta, name=name)
    for idx in clone.indexes:
        if idx.name and not idx.name.endswith(SHADOW_NAME_SUFFIX):
            idx.name = f"{idx.name}{SHADOW_NAME_SUFFIX}"
    for const in clone.constraints:
        # Suffix only explicitly-named UNIQUE constraints (their backing index
        # name is schema-global); PK/CHECK names are table-derived and unique.
        from sqlalchemy import UniqueConstraint

        if isinstance(const, UniqueConstraint) and const.name:
            const.name = f"{const.name}{SHADOW_NAME_SUFFIX}"
    try:
        yield clone
    finally:
        meta.remove(clone)


def _shadow_create_ddl(table: str) -> str:
    """Render CREATE TABLE for ``<table>_p`` from the model (inline constraints only).

    The shadow is built from the current (partitioned) model definition -- not
    from the legacy original -- because the model is the source of truth for the
    new columns (e.g. ``embedding.project_id`` does not exist on the legacy
    table). ``CreateTable`` emits only the inline constraints (PK, unique,
    check, FK); the secondary indexes are built separately in the ``index``
    phase after the backfill.
    """
    with _shadow_clone(table) as clone:
        return str(CreateTable(clone).compile(dialect=_PG_DIALECT)).strip().rstrip(";")


def cmd_create(engine: Engine, tables, giants, dry_run: bool) -> None:
    for table in tables:
        shadow = _shadow(table)
        with engine.begin() as conn:
            if table_relkind(conn, shadow) is not None:
                _log(f"{shadow} already exists; skipping create")
            else:
                _log(f"creating shadow {shadow} (partitioned, no heavy indexes)")
                _exec(conn, _shadow_create_ddl(table), dry_run)
            # Dedicated giant partitions FIRST (empty default => no scan), then
            # the DEFAULT partition for the long tail.
            for pid in giants:
                _log(f"  + dedicated partition {dedicated_partition_name(shadow, pid)}")
                if not dry_run:
                    create_dedicated_partition(conn, shadow, pid)
            _log(f"  + default partition {shadow}_default")
            if not dry_run:
                create_default_partition(conn, shadow)


# --------------------------------------------------------------------------- #
# backfill
# --------------------------------------------------------------------------- #
def _backfill_table(
    engine: Engine,
    table: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    plan = _TABLE_PLANS[table]
    shadow = _shadow(table)
    cols = plan["columns"]
    insert_cols = ", ".join(c for c, _ in cols)
    select_exprs = ", ".join(e for _, e in cols)
    watermark = plan["watermark"]
    wm_col = watermark.split(".", 1)[1]
    extra_where = plan["where"]

    # Resume from the high-water mark already present in the shadow table.
    with engine.connect() as conn:
        start = conn.execute(
            text(f"SELECT COALESCE(MAX({wm_col}), 0) FROM {shadow}"),
        ).scalar()
        src_max = conn.execute(
            text(f"SELECT COALESCE(MAX({watermark.split('.', 1)[1]}), 0) FROM {table}"),
        ).scalar()
    _log(f"{table}: backfilling {wm_col} in ({start}, {src_max}] batch={batch_size}")

    lo = int(start or 0)
    total = 0
    while lo < int(src_max or 0):
        hi = lo + batch_size
        where = f"{watermark} > :lo AND {watermark} <= :hi"
        if extra_where:
            where += f" AND {extra_where}"
        sql = (
            f"INSERT INTO {shadow} ({insert_cols}) "
            f"SELECT {select_exprs} FROM {plan['from_clause']} "
            f"WHERE {where} "
            f"ON CONFLICT DO NOTHING"
        )
        if dry_run:
            _exec(None, sql, dry_run, {"lo": lo, "hi": hi})  # type: ignore[arg-type]
            lo = hi
            continue
        with engine.begin() as conn:
            res = conn.execute(text(sql), {"lo": lo, "hi": hi})
            total += res.rowcount or 0
        if (lo // batch_size) % 20 == 0:
            _log(f"  {table}: {wm_col} <= {hi} ({total} rows so far)")
        lo = hi
    _log(f"{table}: backfill done ({total} rows inserted this run)")


def cmd_backfill(engine: Engine, tables, batch_size: int, dry_run: bool) -> None:
    # log_event MUST be backfilled before the child tables, which derive their
    # project_id by joining it.
    ordered = [t for t in PARTITIONED_TABLES if t in tables]
    for table in ordered:
        _backfill_table(engine, table, batch_size, dry_run)


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
def _all_index_ddl(table: str) -> list[str]:
    with _shadow_clone(table) as clone:
        return [
            str(CreateIndex(idx).compile(dialect=_PG_DIALECT)).strip().rstrip(";")
            for idx in clone.indexes
        ]


def cmd_index(engine: Engine, tables, dry_run: bool) -> None:
    for table in tables:
        ddls = _all_index_ddl(table)
        if not ddls:
            continue
        _log(
            f"{table}: building {len(ddls)} secondary indexes on {_shadow(table)} (offline, slow)"
        )
        for ddl in ddls:
            # Per-partition build: a CREATE INDEX on the partitioned parent
            # recursively builds the index on every child partition. The HNSW
            # build on the giant partitions dominates the wall-clock cost.
            with engine.begin() as conn:
                _exec(conn, ddl, dry_run)
        _log(f"{table}: secondary indexes done")


# --------------------------------------------------------------------------- #
# cutover
# --------------------------------------------------------------------------- #
def cmd_cutover(
    engine: Engine, tables, batch_size: int, dry_run: bool, stamp: bool
) -> None:
    for table in tables:
        shadow = _shadow(table)
        _log(f"{table}: CUTOVER (acquires ACCESS EXCLUSIVE)")
        with engine.begin() as conn:
            _exec(conn, f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE", dry_run)
            # Delta catch-up: anything inserted into the original after the last
            # backfill batch. ON CONFLICT DO NOTHING keeps it idempotent.
            plan = _TABLE_PLANS[table]
            cols = plan["columns"]
            insert_cols = ", ".join(c for c, _ in cols)
            select_exprs = ", ".join(e for _, e in cols)
            where = plan["where"] or "TRUE"
            _exec(
                conn,
                f"INSERT INTO {shadow} ({insert_cols}) "
                f"SELECT {select_exprs} FROM {plan['from_clause']} "
                f"WHERE {where} ON CONFLICT DO NOTHING",
                dry_run,
            )
            _exec(conn, f"ALTER TABLE {table} RENAME TO {_old(table)}", dry_run)
            _exec(conn, f"ALTER TABLE {shadow} RENAME TO {table}", dry_run)
            # Re-point the id sequence so new inserts continue past the max.
            if table in ("log_event", "embedding", "embedding_queue"):
                _exec(
                    conn,
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"GREATEST((SELECT COALESCE(MAX(id), 1) FROM {table}), 1))",
                    dry_run,
                )
        _log(f"{table}: cutover complete (original preserved as {_old(table)})")

    if stamp and not dry_run:
        from alembic import command
        from alembic.config import Config

        _log("stamping alembic revision partition_kernel_by_project")
        command.stamp(Config("alembic.ini"), "partition_kernel_by_project")


# --------------------------------------------------------------------------- #
# verify / rollback / drop-old
# --------------------------------------------------------------------------- #
def cmd_verify(engine: Engine, tables, dry_run: bool) -> None:
    with engine.connect() as conn:
        for table in tables:
            new_kind = table_relkind(conn, table)
            children = child_partitions(conn, table) if new_kind == "p" else []
            new_n = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            old_n = None
            if table_relkind(conn, _old(table)) is not None:
                old_n = conn.execute(
                    text(f"SELECT COUNT(*) FROM {_old(table)}"),
                ).scalar()
            _log(
                f"{table}: relkind={new_kind} children={len(children)} "
                f"rows={new_n} old_rows={old_n}",
            )
            if table == "log_event":
                plan = conn.execute(
                    text(
                        "EXPLAIN SELECT * FROM log_event WHERE project_id = :p LIMIT 1",
                    ),
                    {"p": 189888},
                ).fetchall()
                pruned = any("_default" not in r[0] for r in plan)
                _log(f"  partition pruning visible in plan: {pruned}")


def cmd_rollback(engine: Engine, tables, dry_run: bool) -> None:
    for table in tables:
        _log(f"{table}: ROLLBACK -> restoring {_old(table)}")
        with engine.begin() as conn:
            if table_relkind(conn, table) is not None:
                _exec(conn, f"ALTER TABLE {table} RENAME TO {table}_failed", dry_run)
            _exec(conn, f"ALTER TABLE {_old(table)} RENAME TO {table}", dry_run)


def cmd_drop_old(engine: Engine, tables, dry_run: bool) -> None:
    for table in tables:
        _log(f"{table}: dropping {_old(table)} (irreversible)")
        with engine.begin() as conn:
            _exec(conn, f"DROP TABLE IF EXISTS {_old(table)} CASCADE", dry_run)


def cmd_finalize(engine: Engine, tables, dry_run: bool) -> None:
    """Rename suffixed shadow indexes/constraints back to their canonical names.

    Safe to run only after ``drop-old`` has freed the legacy (schema-global)
    index names. Constraints are renamed first (which also renames their backing
    index in Postgres), then any remaining plain indexes.
    """
    suffix = SHADOW_NAME_SUFFIX
    for table in tables:
        _log(f"{table}: finalizing index/constraint names (stripping {suffix!r})")
        with engine.begin() as conn:
            constraints = (
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = to_regclass(:t) AND conname LIKE :pat",
                    ),
                    {"t": table, "pat": f"%{suffix}"},
                )
                .scalars()
                .all()
                if not dry_run
                else []
            )
            for conname in constraints:
                _exec(
                    conn,
                    f'ALTER TABLE "{table}" RENAME CONSTRAINT "{conname}" '
                    f'TO "{conname.removesuffix(suffix)}"',
                    dry_run,
                )
            indexes = (
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = :t AND indexname LIKE :pat",
                    ),
                    {"t": table, "pat": f"%{suffix}"},
                )
                .scalars()
                .all()
                if not dry_run
                else []
            )
            for idxname in indexes:
                _exec(
                    conn,
                    f'ALTER INDEX "{idxname}" RENAME TO "{idxname.removesuffix(suffix)}"',
                    dry_run,
                )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
_PG_DIALECT = None  # set in main once psycopg2 dialect is importable


def main(argv: list[str] | None = None) -> int:
    global _PG_DIALECT
    from sqlalchemy.dialects import postgresql

    _PG_DIALECT = postgresql.dialect()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phases",
        nargs="+",
        choices=[
            "create",
            "backfill",
            "index",
            "cutover",
            "verify",
            "rollback",
            "drop-old",
            "finalize",
        ],
        help="One or more phases to run, in order.",
    )
    parser.add_argument(
        "--table",
        action="append",
        choices=list(PARTITIONED_TABLES),
        help="Restrict to specific table(s); default = all four.",
    )
    parser.add_argument(
        "--giant-project",
        action="append",
        type=int,
        default=[],
        dest="giants",
        help="project_id to give its own dedicated partition (repeatable).",
    )
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument("--db-url", default=None, help="Override settings.db_url.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-stamp",
        action="store_true",
        help="Skip the alembic stamp at the end of cutover.",
    )
    args = parser.parse_args(argv)

    tables = args.table or list(PARTITIONED_TABLES)
    db_url = args.db_url or str(settings.db_url)
    _log(
        f"db={db_url.split('@')[-1]} tables={tables} giants={args.giants} dry_run={args.dry_run}"
    )
    engine = create_engine(db_url, pool_pre_ping=True)

    for phase in args.phases:
        _log(f"=== phase: {phase} ===")
        if phase == "create":
            cmd_create(engine, tables, args.giants, args.dry_run)
        elif phase == "backfill":
            cmd_backfill(engine, tables, args.batch_size, args.dry_run)
        elif phase == "index":
            cmd_index(engine, tables, args.dry_run)
        elif phase == "cutover":
            cmd_cutover(
                engine, tables, args.batch_size, args.dry_run, not args.no_stamp
            )
        elif phase == "verify":
            cmd_verify(engine, tables, args.dry_run)
        elif phase == "rollback":
            cmd_rollback(engine, tables, args.dry_run)
        elif phase == "drop-old":
            cmd_drop_old(engine, tables, args.dry_run)
        elif phase == "finalize":
            cmd_finalize(engine, tables, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
