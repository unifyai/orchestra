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


# --------------------------------------------------------------------------- #
# In-place conversion of populated legacy (non-partitioned) tables.
# --------------------------------------------------------------------------- #
# Target composite primary keys (the partition key must be part of the PK).
_NEW_PK: dict[str, list[str]] = {
    "log_event": ["project_id", "id"],
    "log_event_context": ["project_id", "log_event_id", "context_id"],
    "embedding": ["project_id", "id"],
    "embedding_queue": ["project_id", "id"],
}
# Child table -> the column whose value joins to ``log_event.id`` to derive the
# denormalized ``project_id`` during backfill.
_PROJECT_BACKFILL_FK: dict[str, str] = {
    "log_event_context": "log_event_id",
    "embedding": "ref_id",
    "embedding_queue": "ref_id",
}
# Suffix applied to the legacy table's pre-existing indexes so the freshly
# created partitioned parent can claim the canonical (schema-global) index
# names; the legacy indexes are then attached to the parent's partitioned
# indexes by definition match (no rebuild).
_PRE_PARTITION_SUFFIX = "_prepart"


def _constraints(conn: Connection, table: str, contype: str) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t) AND contype = :ct",
            ),
            {"t": table, "ct": contype},
        ).scalars(),
    )


def _fks_referencing(conn: Connection, target: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT conrelid::regclass::text AS tbl, conname FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = to_regclass(:t)",
        ),
        {"t": target},
    ).all()
    return [(r[0], r[1]) for r in rows]


def _column_names(conn: Connection, table: str) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = to_regclass(:t) AND attnum > 0 AND NOT attisdropped "
                "ORDER BY attnum",
            ),
            {"t": table},
        ).scalars(),
    )


def _index_names(conn: Connection, table: str) -> list[str]:
    return list(
        conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
            {"t": table},
        ).scalars(),
    )


def _column_types(conn: Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a WHERE a.attrelid = to_regclass(:t) "
            "AND a.attnum > 0 AND NOT a.attisdropped",
        ),
        {"t": table},
    ).all()
    return {r[0]: r[1] for r in rows}


def _align_partition_column_types(
    conn: Connection,
    parent: str,
    partition: str,
) -> None:
    """ALTER the partition's column types to match the parent before ATTACH.

    Existing tables can have drifted from the model (e.g. a column is ``text``
    in the DB but ``varchar`` in the model). ATTACH PARTITION requires exact
    type equality, so converge the partition to the parent's (model-derived)
    types. For binary-coercible changes (varchar<->text, varchar length
    widening) this is a metadata-only operation with no table rewrite.
    """
    parent_types = _column_types(conn, parent)
    part_types = _column_types(conn, partition)
    for name, ptype in parent_types.items():
        cur = part_types.get(name)
        if cur is not None and cur != ptype:
            conn.execute(
                text(
                    f'ALTER TABLE "{partition}" ALTER COLUMN "{name}" '
                    f'TYPE {ptype} USING "{name}"::{ptype}',
                ),
            )


def _drop_extra_columns(
    conn: Connection,
    parent: str,
    partition: str,
) -> None:
    """Drop columns on the partition that are absent from the parent before ATTACH.

    Legacy tables can carry leftover columns not present in the model (e.g. a
    retired ``tmp_*`` scratch column). ATTACH PARTITION rejects a partition that
    has any column the parent lacks ("The new partition may contain only the
    columns present in parent"), so drop the drift. Columns the model adds but
    the legacy table lacks are handled by the phase-2 backfill, not here.
    """
    parent_cols = set(_column_names(conn, parent))
    for name in _column_names(conn, partition):
        if name not in parent_cols:
            conn.execute(text(f'ALTER TABLE "{partition}" DROP COLUMN "{name}"'))


def convert_legacy_to_partitioned(conn: Connection) -> None:
    """Convert the populated, non-partitioned kernel tables to partitioned form.

    In-place, no bulk data copy and no index rebuild: each legacy table is
    reshaped to the partitioned schema and then attached as the ``DEFAULT``
    partition of a freshly-created partitioned parent, so its existing GIN/HNSW
    indexes ride along. Designed to run inside the alembic migration transaction
    (brief exclusive locks; acceptable with a maintenance window).

    Assumes ``log_event`` carries ``project_id`` already and that the child
    tables derive theirs from it. ``log_unique_constraint`` is not partitioned;
    it only loses its FK to ``log_event`` (handled in phase 1).
    """
    from orchestra.db.meta import meta
    from orchestra.db.models import load_all_models

    load_all_models()

    # Phase 1: drop every FK that points at log_event (its PK is changing to a
    # composite key that single-column FKs can no longer reference).
    for tbl, conname in _fks_referencing(conn, "log_event"):
        conn.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT "{conname}"'))

    # Phase 2: denormalize project_id onto the child tables (log_event intact).
    for child, fk_col in _PROJECT_BACKFILL_FK.items():
        conn.execute(
            text(f'ALTER TABLE "{child}" ADD COLUMN IF NOT EXISTS project_id integer'),
        )
        conn.execute(
            text(
                f'UPDATE "{child}" c SET project_id = le.project_id '
                f"FROM log_event le WHERE c.{fk_col} = le.id AND c.project_id IS NULL",
            ),
        )
        # Rows whose log_event no longer exists (orphans) cannot be placed in a
        # partition; drop them.
        conn.execute(text(f'DELETE FROM "{child}" WHERE project_id IS NULL'))
        conn.execute(
            text(f'ALTER TABLE "{child}" ALTER COLUMN project_id SET NOT NULL'),
        )

    # Phase 3: reshape each table and attach it as the DEFAULT partition.
    for table in PARTITIONED_TABLES:
        default = default_partition_name(table)

        # Drop old PK + unique constraints (replaced by composite ones that
        # include project_id), and log_event's own outbound project FK (the
        # partitioned parent re-declares it).
        for pk in _constraints(conn, table, "p"):
            conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{pk}"'))
        for uq in _constraints(conn, table, "u"):
            conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{uq}"'))
        if table == "log_event":
            for fk in _constraints(conn, table, "f"):
                conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{fk}"'))

        # Ensure the composite-PK columns are NOT NULL; ATTACH auto-creates the
        # parent's PK/UNIQUE indexes on the partition, so we must NOT add our own
        # PK here (that would be a second primary key and ATTACH would reject it).
        for col in _NEW_PK[table]:
            conn.execute(
                text(f'ALTER TABLE "{table}" ALTER COLUMN {col} SET NOT NULL'),
            )

        # Free the canonical (schema-global) index names for the parent by
        # suffixing the legacy table's indexes; they remain valid and get
        # attached to the parent's partitioned indexes by definition match.
        for idx in _index_names(conn, table):
            if not idx.endswith(_PRE_PARTITION_SUFFIX):
                conn.execute(
                    text(
                        f'ALTER INDEX "{idx}" RENAME TO "{idx}{_PRE_PARTITION_SUFFIX}"'
                    ),
                )

        conn.execute(text(f'ALTER TABLE "{table}" RENAME TO "{default}"'))
        meta.tables[table].create(bind=conn)
        # Converge the legacy table to the model-derived parent so ATTACH's
        # exact-shape check passes: drop leftover columns the model no longer
        # has, then align any drifted column types (e.g. text vs varchar).
        _drop_extra_columns(conn, table, default)
        _align_partition_column_types(conn, table, default)
        conn.execute(
            text(f'ALTER TABLE "{table}" ATTACH PARTITION "{default}" DEFAULT'),
        )


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


def table_storage_units(conn: Connection, table: str) -> list[str]:
    """Return the physical relations that hold ``table``'s rows.

    For a partitioned parent that is its child partitions (the parent itself has
    no storage); for an ordinary table it is just the table. Lets maintenance
    (VACUUM/REINDEX) operate per physical segment so one giant partition cannot
    block the rest.
    """
    if is_partitioned(conn, table):
        return child_partitions(conn, table)
    return [table] if relation_exists(conn, table) else []


def find_promotion_candidates(
    conn: Connection,
    threshold: int,
) -> list[tuple[int, int]]:
    """Projects still in the DEFAULT partition large enough to deserve their own.

    Returns ``(project_id, log_event_row_count)`` pairs sorted by size desc.
    Counted against ``log_event``'s DEFAULT partition only, so projects already
    promoted to a dedicated partition are naturally excluded.
    """
    default_part = default_partition_name("log_event")
    if not relation_exists(conn, default_part):
        return []
    rows = conn.execute(
        text(
            f'SELECT project_id, count(*) AS c FROM "{default_part}" '
            f"GROUP BY project_id HAVING count(*) >= :t ORDER BY c DESC",
        ),
        {"t": threshold},
    ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


def promote_project_to_partition(
    conn: Connection,
    project_id: int,
) -> list[str]:
    """Move a project's rows out of DEFAULT into its own dedicated partition.

    For each table in the heavy family that does not already have a dedicated
    partition for ``project_id``: build a standalone table, move the project's
    rows out of the DEFAULT partition into it, then ATTACH it (which builds the
    parent's per-partition GIN/HNSW/btree indexes on the new partition). This is
    a per-row move, so it is meant to run proactively while the project is at
    the promotion threshold -- not once it is already enormous. Returns the list
    of partitions created.

    Run inside a transaction: the DELETE-from-default + ATTACH must be atomic.
    """
    pid = int(project_id)
    created: list[str] = []
    for table in PARTITIONED_TABLES:
        partition = dedicated_partition_name(table, pid)
        if relation_exists(conn, partition):
            continue
        default_part = default_partition_name(table)
        # Standalone clone: columns + NOT NULL + defaults + CHECK constraints
        # (ATTACH requires the partition to carry the parent's CHECK constraints
        # to skip a validation scan). PK/unique/secondary indexes are built by
        # ATTACH from the parent's partitioned indexes.
        conn.execute(
            text(
                f'CREATE TABLE "{partition}" '
                f'(LIKE "{table}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
            ),
        )
        # Move the project's rows directly into the standalone table (inserting
        # into the parent would just route them back to DEFAULT). Use an explicit
        # column list -- a partition's physical column order can differ from the
        # parent's (e.g. when project_id was appended via ALTER ADD COLUMN), so a
        # positional ``SELECT *`` would misalign columns.
        cols = ", ".join(f'"{c}"' for c in _column_names(conn, table))
        conn.execute(
            text(
                f"WITH moved AS ("
                f'  DELETE FROM "{default_part}" WHERE project_id = {pid} RETURNING *'
                f') INSERT INTO "{partition}" ({cols}) SELECT {cols} FROM moved',
            ),
        )
        conn.execute(
            text(
                f'ALTER TABLE "{table}" ATTACH PARTITION "{partition}" '
                f"FOR VALUES IN ({pid})",
            ),
        )
        created.append(partition)
    return created
