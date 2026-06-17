"""Shared DDL helpers for the LIST(project_id)-partitioned kernel tables.

The heavy kernel tables (``log_event``, ``log_event_context``, ``embedding``,
``embedding_queue``) are partitioned ``BY LIST (project_id)``. A single
``DEFAULT`` partition holds the long tail of small projects (where ordinary
row-by-row deletes are already cheap); giant projects get their own dedicated
partition so deleting one is an O(1) ``DROP TABLE`` of the partition with no
per-row GIN/HNSW index maintenance.

This module is the single source of truth for partition naming and DDL. It is
reused by:

* the alembic migration that provisions partitions on a fresh deploy,
* the create-backfill-swap runbook that converts populated legacy tables,
* the test harness (``conftest``) that needs a DEFAULT partition after
  ``meta.create_all``,
* the per-project "drop fast" deletion path and the index-maintenance worker.

All identifiers are derived from the fixed ``PARTITIONED_TABLES`` tuple or from
integer ``project_id`` values, so the string interpolation here is not an
injection surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

# The heavy kernel family partitioned by LIST (project_id). ``log_event`` is the
# parent of the conceptual family; the other three denormalize ``project_id``
# from it so each can carry its own per-partition indexes and be dropped with
# the project.
PARTITIONED_TABLES: tuple[str, ...] = (
    "log_event",
    "log_event_context",
    "embedding",
    "embedding_queue",
)


def default_partition_name(table: str) -> str:
    """Name of the catch-all DEFAULT partition for ``table``."""
    return f"{table}_default"


def dedicated_partition_name(table: str, project_id: int) -> str:
    """Name of the dedicated partition holding a single giant project's rows."""
    return f"{table}_p{int(project_id)}"


def table_relkind(conn: Connection, table: str) -> str | None:
    """Return ``pg_class.relkind`` for ``table`` in the public schema.

    ``'p'`` -> partitioned parent, ``'r'`` -> ordinary table, ``None`` -> the
    relation does not exist.
    """
    return conn.execute(
        text(
            "SELECT relkind FROM pg_class "
            "WHERE relname = :t AND relnamespace = 'public'::regnamespace",
        ),
        {"t": table},
    ).scalar()


def is_partitioned(conn: Connection, table: str) -> bool:
    """True if ``table`` is a partitioned parent (relkind ``'p'``)."""
    return table_relkind(conn, table) == "p"


def relation_exists(conn: Connection, relation: str) -> bool:
    """True if a relation (table or partition) with this name exists."""
    return table_relkind(conn, relation) is not None


def table_has_rows(conn: Connection, table: str) -> bool:
    """Cheap existence probe: does ``table`` contain at least one row?

    ``table`` always comes from :data:`PARTITIONED_TABLES`, so the f-string is
    safe.
    """
    return bool(
        conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")).scalar(),
    )


def existing_project_ids(
    conn: Connection,
    project_ids: Iterable[int],
) -> list[int]:
    """Filter ``project_ids`` down to those that exist in the ``project`` table.

    Used so the migration only carves out a dedicated partition for a giant
    project that actually exists in the current environment (the giant id list
    is prod-specific; on staging/CI those ids are absent and fall through to
    the DEFAULT partition).
    """
    ids = [int(p) for p in project_ids]
    if not ids:
        return []
    rows = conn.execute(
        text("SELECT id FROM project WHERE id = ANY(:ids)"),
        {"ids": ids},
    ).scalars()
    return list(rows)


def create_default_partition(conn: Connection, table: str) -> None:
    """Create the DEFAULT partition for ``table`` if it does not already exist."""
    name = default_partition_name(table)
    conn.execute(
        text(f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table}" DEFAULT'),
    )


def create_dedicated_partition(
    conn: Connection,
    table: str,
    project_id: int,
) -> None:
    """Create a dedicated single-project partition for ``table`` if absent.

    NOTE: when a DEFAULT partition already exists, Postgres validates that the
    default holds no rows routing to ``project_id`` by taking an ACCESS
    EXCLUSIVE lock and scanning it. On an empty/freshly-created schema this is
    instant; for a populated table the backfill-cutover runbook creates the
    dedicated partition up front (before any rows land in DEFAULT) instead.
    """
    pid = int(project_id)
    name = dedicated_partition_name(table, pid)
    conn.execute(
        text(
            f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table}" FOR VALUES IN ({pid})'
        ),
    )


def ensure_partitions(
    conn: Connection,
    table: str,
    dedicated_project_ids: Sequence[int] = (),
) -> None:
    """Ensure ``table`` has its dedicated partitions and a DEFAULT partition.

    Dedicated partitions are created first so the (empty) DEFAULT-partition
    scan that Postgres performs when attaching a ``FOR VALUES IN`` partition is
    avoided entirely on fresh schemas.
    """
    for pid in dedicated_project_ids:
        create_dedicated_partition(conn, table, pid)
    create_default_partition(conn, table)


def project_has_dedicated_partition(conn: Connection, project_id: int) -> bool:
    """True if ``project_id`` owns its own (giant) partition rather than DEFAULT.

    ``log_event`` is the anchor of the partitioned family -- a giant project is
    always promoted across the whole family together (migration / runbook /
    maintenance worker), so the presence of its ``log_event`` partition implies
    the rest exist too.
    """
    return relation_exists(conn, dedicated_partition_name("log_event", project_id))


def drop_project_partitions(conn: Connection, project_id: int) -> list[str]:
    """Detach and drop every dedicated partition owned by ``project_id``.

    This is the O(1) tenant-deletion primitive: a giant project's rows (and
    their per-partition GIN/HNSW index segments) are removed as a metadata
    operation, with no per-row index maintenance. A no-op for a project that
    lives in the DEFAULT partition (returns an empty list), so callers can
    invoke it unconditionally and fall back to row-level deletion when nothing
    was dropped.
    """
    dropped: list[str] = []
    for table in PARTITIONED_TABLES:
        partition = dedicated_partition_name(table, project_id)
        if not relation_exists(conn, partition):
            continue
        conn.execute(text(f'ALTER TABLE "{table}" DETACH PARTITION "{partition}"'))
        conn.execute(text(f'DROP TABLE IF EXISTS "{partition}"'))
        dropped.append(partition)
    return dropped


def drop_partitions_for_owned_projects(
    conn: Connection,
    *,
    user_id: str | None = None,
    organization_id: int | None = None,
) -> dict[int, list[str]]:
    """Drop dedicated partitions for every giant project owned by a user/org.

    Called just before a user/organization cascade-delete so the heavy projects
    are removed via O(1) partition drops; the subsequent cascade then only has
    to delete the small metadata (and the row-level data of any non-giant
    projects, which live in the DEFAULT partition and are cheap). Returns a map
    of project_id -> dropped partition names for logging.
    """
    if user_id is not None:
        rows = (
            conn.execute(
                text("SELECT id FROM project WHERE user_id = :u"),
                {"u": user_id},
            )
            .scalars()
            .all()
        )
    elif organization_id is not None:
        rows = (
            conn.execute(
                text("SELECT id FROM project WHERE organization_id = :o"),
                {"o": organization_id},
            )
            .scalars()
            .all()
        )
    else:
        return {}

    dropped: dict[int, list[str]] = {}
    for project_id in rows:
        parts = drop_project_partitions(conn, project_id)
        if parts:
            dropped[project_id] = parts
    return dropped


def child_partitions(conn: Connection, table: str) -> list[str]:
    """Return the names of all child partitions currently attached to ``table``."""
    rows = conn.execute(
        text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class child ON child.oid = pg_inherits.inhrelid
            WHERE parent.relname = :t
              AND parent.relnamespace = 'public'::regnamespace
            ORDER BY child.relname
            """,
        ),
        {"t": table},
    ).scalars()
    return list(rows)
