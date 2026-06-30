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

import hashlib
from collections.abc import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

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
    """Return ``pg_class.relkind`` for ``table``, resolved via the search_path.

    ``'p'`` -> partitioned parent, ``'r'`` -> ordinary table, ``None`` -> the
    relation does not exist. Kernel tables live in ``public`` (always on the
    search_path), so this matches them in production while remaining usable
    against a scratch schema.
    """
    return conn.execute(
        text("SELECT relkind FROM pg_class WHERE oid = to_regclass(:t)"),
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
            f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{table}" FOR VALUES IN ({pid})',
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
                        f'ALTER INDEX "{idx}" RENAME TO "{idx}{_PRE_PARTITION_SUFFIX}"',
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

        # meta.create() minted a fresh identity sequence for the parent's ``id``
        # (the legacy sequence name was still held by the renamed default), so it
        # restarts at 1 while the attached data carries the legacy id range.
        # Advance it past the existing max so inserts via the parent don't
        # collide with existing rows.
        seq = conn.execute(
            text("SELECT pg_get_serial_sequence(:t, 'id')"),
            {"t": table},
        ).scalar()
        if seq is not None:
            max_id = conn.execute(
                text(f'SELECT max(id) FROM "{table}"'),
            ).scalar()
            if max_id is not None:
                conn.execute(
                    text("SELECT setval(:s, :m, true)"),
                    {"s": seq, "m": int(max_id)},
                )

    # The freshly attached partitions are huge; without size-independent
    # autovacuum thresholds their planner stats go stale and the hot queries
    # seq-scan the whole partition. Apply the tuning as part of the conversion.
    tune_partition_storage(conn)


# --------------------------------------------------------------------------- #
# Owner sub-partitioning (phase 2): divide the shared Assistants project's
# partition by owner_key so an assistant/team can be dropped as an O(1) sub-
# partition. Only the deletion-relevant heavy family is sub-partitioned;
# embedding_queue is transient and stays project-only.
# --------------------------------------------------------------------------- #
OWNER_SUB_TABLES: tuple[str, ...] = (
    "log_event",
    "log_event_context",
    "embedding",
)
# Composite PKs once owner_key participates (it must, to sub-partition by it).
# owner_key is last to match the model's column-declaration order, so a fresh
# create_all and the migration produce an identical primary key.
_OWNER_SUB_PK: dict[str, list[str]] = {
    "log_event": ["project_id", "id", "owner_key"],
    "log_event_context": ["project_id", "log_event_id", "context_id", "owner_key"],
    "embedding": ["project_id", "id", "owner_key"],
}


def owner_subpartition_name(table: str, project_id: int, owner_key: str) -> str:
    """Name of the per-owner sub-partition under a project's partition."""
    return f"{dedicated_partition_name(table, project_id)}_{owner_key}"


def add_owner_key_to_keys(conn: Connection, table: str) -> None:
    """Make owner_key NOT NULL and part of the PK + every UNIQUE constraint.

    Every unique constraint on a partitioned table must contain all partition
    key columns, so owner_key is added to the PK and to each existing UNIQUE
    (after project_id). owner_key is functionally determined by the log's
    ``ref_id`` / owning context, so this does not change real uniqueness.
    """
    conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN owner_key SET NOT NULL'))
    for conname, condef in conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t) AND contype = 'u'",
        ),
        {"t": table},
    ).all():
        cols = [
            x.strip()
            for x in condef[condef.index("(") + 1 : condef.rindex(")")].split(",")
        ]
        new_cols = ["project_id", "owner_key"] + [
            x for x in cols if x not in ("project_id", "owner_key")
        ]
        conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{conname}"'))
        conn.execute(
            text(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" '
                f'UNIQUE ({", ".join(new_cols)})',
            ),
        )
    pkname = conn.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t) AND contype = 'p'",
        ),
        {"t": table},
    ).scalar()
    conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{pkname}"'))
    conn.execute(
        text(
            f'ALTER TABLE "{table}" ADD PRIMARY KEY ({", ".join(_OWNER_SUB_PK[table])})',
        ),
    )


def owner_key_in_pk(conn: Connection, table: str) -> bool:
    """True if ``owner_key`` is already part of ``table``'s primary key."""
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                "AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = to_regclass(:t) AND c.contype = 'p' "
                "AND a.attname = 'owner_key')",
            ),
            {"t": table},
        ).scalar(),
    )


def remove_owner_key_from_keys(conn: Connection, table: str) -> None:
    """Inverse of :func:`add_owner_key_to_keys` (PK/unique drop owner_key)."""
    for conname, condef in conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t) AND contype = 'u'",
        ),
        {"t": table},
    ).all():
        cols = [
            x.strip()
            for x in condef[condef.index("(") + 1 : condef.rindex(")")].split(",")
        ]
        new_cols = [x for x in cols if x != "owner_key"]
        conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{conname}"'))
        conn.execute(
            text(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" '
                f'UNIQUE ({", ".join(new_cols)})',
            ),
        )
    pkname = conn.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = to_regclass(:t) AND contype = 'p'",
        ),
        {"t": table},
    ).scalar()
    pk_cols = [c for c in _OWNER_SUB_PK[table] if c != "owner_key"]
    conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{pkname}"'))
    conn.execute(text(f'ALTER TABLE "{table}" ADD PRIMARY KEY ({", ".join(pk_cols)})'))
    conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN owner_key DROP NOT NULL'))


def sub_partition_project_by_owner(conn: Connection, project_id: int) -> None:
    """Carve a project's rows into a ``PARTITION BY LIST(owner_key)`` partition.

    Replaces the project's leaf placement (its rows currently live in the
    top-level DEFAULT partition) with a dedicated partition that is itself
    sub-partitioned by owner_key, starting with just a DEFAULT sub-partition;
    all rows land there until specific owners are promoted. Assumes
    :func:`add_owner_key_to_keys` has already run.
    """
    pid = int(project_id)
    for table in OWNER_SUB_TABLES:
        top_default = default_partition_name(table)
        sub_parent = dedicated_partition_name(table, pid)
        conn.execute(
            text(
                f'CREATE TABLE "{sub_parent}" '
                f'(LIKE "{table}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS) '
                f"PARTITION BY LIST (owner_key)",
            ),
        )
        conn.execute(
            text(
                f'CREATE TABLE "{sub_parent}_default" PARTITION OF "{sub_parent}" DEFAULT',
            ),
        )
        cols = ", ".join(f'"{c}"' for c in _column_names(conn, table))
        conn.execute(
            text(
                f'WITH moved AS (DELETE FROM "{top_default}" '
                f"WHERE project_id = {pid} RETURNING *) "
                f'INSERT INTO "{sub_parent}" ({cols}) SELECT {cols} FROM moved',
            ),
        )
        conn.execute(
            text(
                f'ALTER TABLE "{table}" ATTACH PARTITION "{sub_parent}" '
                f"FOR VALUES IN ({pid})",
            ),
        )


def promote_owner(conn: Connection, project_id: int, owner_key: str) -> list[str]:
    """Carve one owner out of a project's sub-DEFAULT into its own sub-partition.

    Enables an O(1) drop of that owner later. Idempotent: skips a table whose
    owner sub-partition already exists. Returns the sub-partitions created.
    """
    pid = int(project_id)
    created: list[str] = []
    for table in OWNER_SUB_TABLES:
        sub_parent = dedicated_partition_name(table, pid)
        part = owner_subpartition_name(table, pid, owner_key)
        if relation_exists(conn, part):
            continue
        sub_default = f"{sub_parent}_default"
        conn.execute(
            text(
                f'CREATE TABLE "{part}" (LIKE "{sub_parent}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
            ),
        )
        cols = ", ".join(f'"{c}"' for c in _column_names(conn, table))
        conn.execute(
            text(
                f'WITH moved AS (DELETE FROM "{sub_default}" '
                f"WHERE owner_key = :ok RETURNING *) "
                f'INSERT INTO "{part}" ({cols}) SELECT {cols} FROM moved',
            ),
            {"ok": owner_key},
        )
        conn.execute(
            text(
                f'ALTER TABLE "{sub_parent}" ATTACH PARTITION "{part}" '
                f"FOR VALUES IN ('{owner_key}')",
            ),
        )
        created.append(part)
    return created


def drop_owner(conn: Connection, project_id: int, owner_key: str) -> str:
    """Delete all of an owner's data within a project.

    O(1) ``DROP PARTITION`` when the owner has a dedicated sub-partition;
    otherwise a row-delete from the project's sub-DEFAULT (cheap for the small
    assistants/teams that were never promoted). Returns the method used.
    """
    pid = int(project_id)
    if relation_exists(conn, owner_subpartition_name("log_event", pid, owner_key)):
        for table in OWNER_SUB_TABLES:
            sub_parent = dedicated_partition_name(table, pid)
            part = owner_subpartition_name(table, pid, owner_key)
            if not relation_exists(conn, part):
                continue
            conn.execute(text(f'ALTER TABLE "{sub_parent}" DETACH PARTITION "{part}"'))
            conn.execute(text(f'DROP TABLE IF EXISTS "{part}"'))
        return "drop_partition"

    # Not promoted: delete by (project_id, owner_key) through the parent, which
    # routes to whichever partition holds the rows. Cheap for small owners.
    for table in OWNER_SUB_TABLES:
        conn.execute(
            text(f'DELETE FROM "{table}" WHERE project_id = :pid AND owner_key = :ok'),
            {"pid": pid, "ok": owner_key},
        )
    return "row_delete"


def index_attached_to_child(
    conn: Connection,
    parent_index: str,
    child_table: str,
) -> bool:
    """True if an index on ``child_table`` is already attached to ``parent_index``.

    Covers both the fresh-deploy case (Postgres auto-created and auto-attached a
    child index when the partition was created from a parent that already
    carried the index) and a resumed/partial index build.
    """
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM pg_inherits inh "
                "  JOIN pg_class ci ON ci.oid = inh.inhrelid "
                "  JOIN pg_class pi ON pi.oid = inh.inhparent "
                "  JOIN pg_index ix ON ix.indexrelid = ci.oid "
                "  JOIN pg_class ct ON ct.oid = ix.indrelid "
                "  WHERE pi.relname = :pidx AND ct.relname = :child)",
            ),
            {"pidx": parent_index, "child": child_table},
        ).scalar(),
    )


def build_partitioned_index(
    conn: Connection,
    relation: str,
    index_name: str,
    columns: str,
    *,
    leaf_suffix: str,
    concurrently: bool = True,
) -> None:
    """Build ``index_name`` on partitioned ``relation`` without long write locks.

    A plain ``CREATE INDEX`` on a partitioned parent recurses into every child
    while holding ACCESS EXCLUSIVE on the whole tree. Instead this registers an
    *invalid* index on the parent only (``ON ONLY``), builds a copy on each leaf
    partition (``CONCURRENTLY`` in production), and attaches it; the parent
    index flips to valid once every leaf is attached. Intermediate partitioned
    children are handled by recursion. Idempotent and resumable: a child whose
    index is already attached is skipped (so a re-run, or the fresh-deploy case
    where partitions auto-inherit the parent's index, is a no-op).

    ``concurrently`` must be False when running inside an open transaction
    (``CREATE INDEX CONCURRENTLY`` forbids one); production migrations run it in
    an autocommit block with ``concurrently=True``. ``columns`` is a trusted
    SQL column list (e.g. ``'"project_id", "owner_key"'``) and ``leaf_suffix``
    derives the per-partition index names.
    """
    conn.execute(
        text(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON ONLY "{relation}" ({columns})',
        ),
    )
    create_kw = "CONCURRENTLY " if concurrently else ""
    for child in child_partitions(conn, relation):
        if index_attached_to_child(conn, index_name, child):
            continue
        leaf_index = f"{child}_{leaf_suffix}"
        if is_partitioned(conn, child):
            build_partitioned_index(
                conn,
                child,
                leaf_index,
                columns,
                leaf_suffix=leaf_suffix,
                concurrently=concurrently,
            )
        else:
            conn.execute(
                text(
                    f'CREATE INDEX {create_kw}IF NOT EXISTS "{leaf_index}" '
                    f'ON "{child}" ({columns})',
                ),
            )
        conn.execute(
            text(f'ALTER INDEX "{index_name}" ATTACH PARTITION "{leaf_index}"'),
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


# --------------------------------------------------------------------------- #
# Per-partition autovacuum / statistics tuning.
#
# The kernel partitions are attached as multi-GB, tens-of-millions-of-rows
# leaves. Under Postgres' default scale-factor thresholds (vacuum 0.2 / analyze
# 0.1) their trigger points sit in the millions of row-changes, so autovacuum
# and autoanalyze effectively never fire on them: planner statistics freeze at
# the value captured when the partition was attached, dead tuples accumulate,
# and the hot context-log queries degrade into whole-partition sequential scans
# that saturate the database. Fixed (size-independent) thresholds keep
# maintenance running at a predictable cadence however large the partition
# grows, and cost_delay=0 stops these latency-critical tables being throttled by
# the autovacuum cost limiter.
# --------------------------------------------------------------------------- #
PARTITION_AUTOVACUUM_RELOPTIONS: dict[str, str] = {
    "autovacuum_vacuum_scale_factor": "0",
    "autovacuum_vacuum_threshold": "50000",
    "autovacuum_vacuum_insert_scale_factor": "0",
    "autovacuum_vacuum_insert_threshold": "50000",
    "autovacuum_analyze_scale_factor": "0",
    "autovacuum_analyze_threshold": "50000",
    "autovacuum_vacuum_cost_delay": "0",
}

# Postgres keeps no per-key statistics for a JSONB column, so the planner
# estimates predicates over ``log_event.data`` (``data @> ...``, ``data ? ...``)
# from the column's whole-document sample. A higher statistics target makes
# those estimates markedly less likely to collapse into a sequential scan.
LOG_EVENT_DATA_STATISTICS_TARGET = 1000


def partition_leaves(conn: Connection, table: str) -> list[str]:
    """Every physical leaf relation under ``table`` (recursing sub-partitions).

    Unlike :func:`table_storage_units` (direct children only) this descends the
    full partition tree, so it also returns the owner sub-partition leaves under
    a project's ``PARTITION BY LIST (owner_key)`` partition. Names come back
    already-quoted/schema-qualified via ``regclass``.
    """
    if not is_partitioned(conn, table):
        return [table] if relation_exists(conn, table) else []
    rows = conn.execute(
        text(
            "SELECT relid::regclass::text FROM pg_partition_tree(cast(:t AS regclass)) "
            "WHERE isleaf",
        ),
        {"t": table},
    ).scalars()
    return list(rows)


def tune_partition_storage(conn: Connection) -> list[str]:
    """Apply autovacuum/statistics tuning to every heavy-kernel leaf partition.

    Idempotent and self-healing: re-running re-asserts the settings, so leaves
    created later by promotion pick up the tuning on the next maintenance pass
    without threading a call through every partition-creation site. Returns the
    leaf relations that were tuned.
    """
    reloptions = ", ".join(
        f"{k} = {v}" for k, v in PARTITION_AUTOVACUUM_RELOPTIONS.items()
    )
    tuned: list[str] = []
    for table in PARTITIONED_TABLES:
        for leaf in partition_leaves(conn, table):
            conn.execute(text(f"ALTER TABLE {leaf} SET ({reloptions})"))
            tuned.append(leaf)
    # Only the log_event family carries the queried JSONB ``data`` column.
    for leaf in partition_leaves(conn, "log_event"):
        conn.execute(
            text(
                f"ALTER TABLE {leaf} ALTER COLUMN data "
                f"SET STATISTICS {LOG_EVENT_DATA_STATISTICS_TARGET}",
            ),
        )
    return tuned


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


def find_owner_promotion_candidates(
    conn: Connection,
    project_id: int,
    threshold: int,
) -> list[tuple[str, int]]:
    """Owners big enough to deserve their own sub-partition within a project.

    Counted against the project's owner sub-DEFAULT only (so already-promoted
    owners are naturally excluded), and ``sys`` is skipped -- it is the
    unclassified/system bucket, not a per-owner deletion unit. Returns
    ``(owner_key, row_count)`` pairs sorted by size desc. Empty when the project
    has not been owner sub-partitioned yet.
    """
    sub_default = f"{dedicated_partition_name('log_event', project_id)}_default"
    if not relation_exists(conn, sub_default):
        return []
    rows = conn.execute(
        text(
            f'SELECT owner_key, count(*) AS c FROM "{sub_default}" '
            f"WHERE owner_key <> 'sys' "
            f"GROUP BY owner_key HAVING count(*) >= :t ORDER BY c DESC",
        ),
        {"t": threshold},
    ).all()
    return [(str(r[0]), int(r[1])) for r in rows]


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


# --------------------------------------------------------------------------- #
# Online (non-locking) promotion.
#
# ``promote_project_to_partition`` / ``promote_owner`` move a tenant's rows and
# ATTACH in a single transaction; the ATTACH then builds the parent's GIN/HNSW
# indexes on the new partition while holding ACCESS EXCLUSIVE. For a tenant the
# size of the shared Assistants project (millions of rows, >1M embeddings) that
# index build runs for tens of minutes -- an unacceptable write outage.
#
# The online variants below split the work so the only app-facing lock is a
# brief one at the very end:
#   1. create   -- an empty, *unattached* standalone leaf (cloned columns +
#                  CHECK + a composite PK so backfill/delta can dedup with
#                  ON CONFLICT DO NOTHING); no heavy indexes.
#   2. backfill -- batched copy of the tenant's rows out of the source DEFAULT
#                  into the leaf. The rows stay live in the source, so reads and
#                  writes are unaffected; resumable from the leaf's high-water
#                  mark.
#   3. index    -- build the parent's heavy (non-unique) secondary indexes on
#                  the leaf. It is unattached/invisible, so a plain CREATE INDEX
#                  locks only this private table; no app-facing lock.
#   4. cutover  -- the only app-facing lock, and a short one: ACCESS EXCLUSIVE on
#                  the source DEFAULT while we catch the delta, DELETE the
#                  tenant's rows from the source and ATTACH the (pre-indexed)
#                  leaf -- all atomic. Because the heavy indexes already match
#                  the parent's partitioned indexes by definition, ATTACH
#                  *attaches* them instead of rebuilding; only the cheap PK and
#                  UNIQUE indexes are (re)built under the lock.
#
# These take an ``Engine`` (not a single ``Connection``) because each phase is
# its own transaction.
# --------------------------------------------------------------------------- #
def _partition_is_attached(conn: Connection, parent: str, partition: str) -> bool:
    """True if ``partition`` is currently an attached child of ``parent``."""
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_inherits i "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE p.relname = :p AND c.relname = :c)",
            ),
            {"p": parent, "c": partition},
        ).scalar(),
    )


def _short_index_name(partition: str, ordinal: int) -> str:
    """A schema-unique index name for ``partition``'s pre-built secondary index.

    Index names are schema-global and capped at 63 chars; an owner sub-partition
    name (``log_event_p189888_<owner>``) can be long, so fall back to a hashed
    form when ``<partition>_si<n>`` would overflow.
    """
    name = f"{partition}_si{ordinal}"
    if len(name) <= 63:
        return name
    digest = hashlib.md5(partition.encode()).hexdigest()[:12]
    return f"idx_{digest}_si{ordinal}"


def _heavy_secondary_index_defs(conn: Connection, table: str) -> list[str]:
    """``pg_get_indexdef`` for ``table``'s non-PK, non-UNIQUE secondary indexes.

    These are the only genuinely expensive builds (GIN on ``data``, the per-
    provider HNSW vector indexes, plain btrees). The PK and UNIQUE indexes are
    cheap btrees left for ATTACH to build under the brief cutover lock; the PK
    is added separately so backfill/delta can dedup. Reading the *actual* index
    definitions (rather than re-rendering the ORM model) means a migration-added
    index absent from the model is still reproduced, and the reproduced index
    matches the parent's partitioned index byte-for-byte so ATTACH attaches it
    instead of rebuilding under lock.
    """
    return list(
        conn.execute(
            text(
                "SELECT pg_get_indexdef(x.indexrelid) "
                "FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "WHERE t.relname = :p "
                "AND NOT x.indisprimary AND NOT x.indisunique "
                "ORDER BY i.relname",
            ),
            {"p": table},
        ).scalars(),
    )


def _retarget_indexdef(
    indexdef: str,
    new_name: str,
    target_table: str,
    *,
    only: bool = False,
) -> str:
    """Rewrite a ``pg_get_indexdef`` string to a new index name + target table.

    ``pg_get_indexdef`` is ``CREATE [UNIQUE] INDEX <name> ON [ONLY] <ref> USING
    <am> (<cols>) [WITH (...)] [WHERE ...]``. Everything from ``USING`` onwards
    (columns, opclass, storage params, partial predicate) is preserved verbatim
    so the rebuilt index is definitionally identical; only the leading name and
    target relation are swapped. ``only=True`` emits ``ON ONLY`` to register an
    (initially invalid) index on a partitioned parent without building it.
    """
    head, _, after_on = indexdef.partition(" ON ")
    create_kw = head[: head.rindex(" INDEX ") + len(" INDEX ")]
    tail = after_on[after_on.find(" USING ") :]
    only_kw = "ONLY " if only else ""
    # IF NOT EXISTS keeps the offline index build resumable: a re-run after an
    # interrupted promotion skips indexes already created (names are derived
    # deterministically from the partition name).
    return f'{create_kw}IF NOT EXISTS "{new_name}" ON {only_kw}"{target_table}"{tail}'


def _has_primary_key(conn: Connection, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t) AND contype = 'p')",
            ),
            {"t": table},
        ).scalar(),
    )


def _primary_key_columns(conn: Connection, table: str) -> list[str]:
    """The ordered PK column names of ``table`` (its live definition).

    Read from the catalog rather than a hardcoded map so the standalone leaf's
    PK matches the parent's *current* PK exactly (the kernel PK gained
    ``owner_key`` when owner sub-partitioning landed); a mismatched PK would make
    ATTACH reject or rebuild the index under lock.
    """
    return list(
        conn.execute(
            text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
                "JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                "WHERE c.conrelid = to_regclass(:t) AND c.contype = 'p' "
                "ORDER BY k.ord",
            ),
            {"t": table},
        ).scalars(),
    )


def _online_promote_leaf(
    engine: Engine,
    *,
    parent: str,
    partition: str,
    for_values_sql: str,
    source_partition: str,
    where_sql: str,
    where_params: dict,
    id_col: str,
    batch_size: int,
) -> bool:
    """Online create+backfill+index+cutover of one leaf partition. See module note.

    ``id_col`` is the monotonically-increasing column used to page the backfill.
    Returns True if this call attached the partition (False if already attached).
    """
    with engine.connect() as conn:
        if relation_exists(conn, partition) and _partition_is_attached(
            conn,
            parent,
            partition,
        ):
            return False

    # Phase 1: standalone leaf (+ PK for ON CONFLICT dedup), no heavy indexes.
    with engine.begin() as conn:
        if not relation_exists(conn, partition):
            pk_cols = _primary_key_columns(conn, parent)
            conn.execute(
                text(
                    f'CREATE TABLE "{partition}" '
                    f'(LIKE "{parent}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
                ),
            )
            conn.execute(
                text(
                    f'ALTER TABLE "{partition}" ADD PRIMARY KEY ({", ".join(pk_cols)})',
                ),
            )
        cols = ", ".join(f'"{c}"' for c in _column_names(conn, partition))

    # Phase 2: batched online backfill, resuming from the leaf's high-water mark.
    with engine.connect() as conn:
        start = (
            conn.execute(
                text(f'SELECT COALESCE(MAX("{id_col}"), 0) FROM "{partition}"'),
            ).scalar()
            or 0
        )
        src_max = (
            conn.execute(
                text(
                    f'SELECT COALESCE(MAX("{id_col}"), 0) FROM "{source_partition}" '
                    f"WHERE {where_sql}",
                ),
                where_params,
            ).scalar()
            or 0
        )
    lo = int(start)
    while lo < int(src_max):
        hi = lo + batch_size
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'INSERT INTO "{partition}" ({cols}) '
                    f'SELECT {cols} FROM "{source_partition}" '
                    f'WHERE {where_sql} AND "{id_col}" > :lo AND "{id_col}" <= :hi '
                    f"ON CONFLICT DO NOTHING",
                ),
                {**where_params, "lo": lo, "hi": hi},
            )
        lo = hi

    # Phase 3: build the parent's heavy secondary indexes on the unattached leaf
    # (locks only this private table).
    with engine.connect() as conn:
        index_defs = _heavy_secondary_index_defs(conn, parent)
    for ordinal, indexdef in enumerate(index_defs):
        ddl = _retarget_indexdef(
            indexdef,
            _short_index_name(partition, ordinal),
            partition,
        )
        with engine.begin() as conn:
            conn.execute(text(ddl))

    # Phase 4: brief locked cutover -- delta catch-up, remove from source, attach.
    with engine.begin() as conn:
        conn.execute(
            text(f'LOCK TABLE "{source_partition}" IN ACCESS EXCLUSIVE MODE'),
        )
        conn.execute(
            text(
                f'INSERT INTO "{partition}" ({cols}) '
                f'SELECT {cols} FROM "{source_partition}" WHERE {where_sql} '
                f"ON CONFLICT DO NOTHING",
            ),
            where_params,
        )
        conn.execute(
            text(f'DELETE FROM "{source_partition}" WHERE {where_sql}'),
            where_params,
        )
        conn.execute(
            text(
                f'ALTER TABLE "{parent}" ATTACH PARTITION "{partition}" '
                f"{for_values_sql}",
            ),
        )
    return True


def _id_col_for(table: str) -> str:
    """The monotonic column used to page the online backfill of ``table``."""
    return "log_event_id" if table == "log_event_context" else "id"


def promote_project_online(
    engine: Engine,
    project_id: int,
    *,
    batch_size: int = 50_000,
) -> list[str]:
    """Online (non-locking) equivalent of :func:`promote_project_to_partition`.

    Promotes ``project_id`` out of each kernel table's top-level DEFAULT into a
    dedicated partition without the long ATTACH-time index build holding a write
    lock. Idempotent and resumable. Returns the partitions attached by this call.
    """
    pid = int(project_id)
    created: list[str] = []
    for table in PARTITIONED_TABLES:
        partition = dedicated_partition_name(table, pid)
        if _online_promote_leaf(
            engine,
            parent=table,
            partition=partition,
            for_values_sql=f"FOR VALUES IN ({pid})",
            source_partition=default_partition_name(table),
            where_sql="project_id = :pid",
            where_params={"pid": pid},
            id_col=_id_col_for(table),
            batch_size=batch_size,
        ):
            created.append(partition)
    if created:
        with engine.begin() as conn:
            tune_partition_storage(conn)
            for partition in created:
                conn.execute(text(f'ANALYZE "{partition}"'))
    return created


def _build_partitioned_secondary_indexes(
    engine: Engine,
    template_table: str,
    sub_parent: str,
    sub_default: str,
) -> None:
    """Pre-build ``template_table``'s heavy indexes as partitioned indexes on an
    unattached ``sub_parent`` (with its single ``sub_default`` leaf).

    For each heavy index: register it ``ON ONLY sub_parent`` (instant, invalid),
    build the matching leaf index on ``sub_default`` (the expensive build, but on
    a table not yet attached to the live tree -- only ``sub_default`` is locked),
    then ATTACH the leaf index so the parent index flips valid. When ``sub_parent``
    is later attached to ``template_table`` these partitioned indexes match its,
    so the attach does not rebuild them under the live lock.
    """
    with engine.connect() as conn:
        index_defs = _heavy_secondary_index_defs(conn, template_table)
    for ordinal, indexdef in enumerate(index_defs):
        parent_idx = _short_index_name(sub_parent, ordinal)
        leaf_idx = _short_index_name(sub_default, ordinal)
        with engine.begin() as conn:
            conn.execute(
                text(_retarget_indexdef(indexdef, parent_idx, sub_parent, only=True)),
            )
            conn.execute(
                text(_retarget_indexdef(indexdef, leaf_idx, sub_default)),
            )
            # Guarded for resumability: a re-run after the leaf index was already
            # attached would otherwise error.
            if not index_attached_to_child(conn, parent_idx, sub_default):
                conn.execute(
                    text(f'ALTER INDEX "{parent_idx}" ATTACH PARTITION "{leaf_idx}"'),
                )


def sub_partition_project_by_owner_online(
    engine: Engine,
    project_id: int,
    *,
    batch_size: int = 50_000,
) -> list[str]:
    """Online (non-locking) equivalent of :func:`sub_partition_project_by_owner`.

    Carves a project out of the top-level DEFAULT into a dedicated
    ``PARTITION BY LIST (owner_key)`` partition without the ATTACH-time GIN/HNSW
    build holding ACCESS EXCLUSIVE on the live table. The heavy index build runs
    offline on the unattached sub-parent's sub-DEFAULT leaf; only the final
    delta + delete-from-DEFAULT + attach is under a brief lock. Assumes
    :func:`add_owner_key_to_keys` has run. Idempotent and resumable. Returns the
    sub-parents attached by this call.
    """
    pid = int(project_id)
    created: list[str] = []
    for table in OWNER_SUB_TABLES:
        top_default = default_partition_name(table)
        sub_parent = dedicated_partition_name(table, pid)
        sub_default = f"{sub_parent}_default"

        with engine.connect() as conn:
            if relation_exists(conn, sub_parent) and _partition_is_attached(
                conn,
                table,
                sub_parent,
            ):
                continue

        # Phase 1: unattached sub-parent (PARTITION BY LIST owner_key) + its
        # sub-DEFAULT leaf + a composite PK (built instantly on the empty leaf)
        # so backfill/delta can dedup with ON CONFLICT DO NOTHING.
        with engine.begin() as conn:
            if not relation_exists(conn, sub_parent):
                conn.execute(
                    text(
                        f'CREATE TABLE "{sub_parent}" '
                        f'(LIKE "{table}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS) '
                        f"PARTITION BY LIST (owner_key)",
                    ),
                )
            if not relation_exists(conn, sub_default):
                conn.execute(
                    text(
                        f'CREATE TABLE "{sub_default}" '
                        f'PARTITION OF "{sub_parent}" DEFAULT',
                    ),
                )
            if not _has_primary_key(conn, sub_parent):
                pk_cols = _primary_key_columns(conn, table)
                conn.execute(
                    text(
                        f'ALTER TABLE "{sub_parent}" ADD PRIMARY KEY '
                        f'({", ".join(pk_cols)})',
                    ),
                )
            cols = ", ".join(f'"{c}"' for c in _column_names(conn, sub_parent))

        id_col = _id_col_for(table)
        # Phase 2: batched online backfill of the whole project into the sub-parent
        # (routes to the sub-DEFAULT leaf). Rows stay live in top_default.
        with engine.connect() as conn:
            start = (
                conn.execute(
                    text(f'SELECT COALESCE(MAX("{id_col}"), 0) FROM "{sub_parent}"'),
                ).scalar()
                or 0
            )
            src_max = (
                conn.execute(
                    text(
                        f'SELECT COALESCE(MAX("{id_col}"), 0) FROM "{top_default}" '
                        f"WHERE project_id = :pid",
                    ),
                    {"pid": pid},
                ).scalar()
                or 0
            )
        lo = int(start)
        while lo < int(src_max):
            hi = lo + batch_size
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f'INSERT INTO "{sub_parent}" ({cols}) '
                        f'SELECT {cols} FROM "{top_default}" '
                        f'WHERE project_id = :pid AND "{id_col}" > :lo '
                        f'AND "{id_col}" <= :hi ON CONFLICT DO NOTHING',
                    ),
                    {"pid": pid, "lo": lo, "hi": hi},
                )
            lo = hi

        # Phase 3: build the heavy partitioned indexes offline on the sub-parent.
        _build_partitioned_secondary_indexes(engine, table, sub_parent, sub_default)

        # Phase 4: brief locked cutover.
        with engine.begin() as conn:
            conn.execute(text(f'LOCK TABLE "{top_default}" IN ACCESS EXCLUSIVE MODE'))
            conn.execute(
                text(
                    f'INSERT INTO "{sub_parent}" ({cols}) '
                    f'SELECT {cols} FROM "{top_default}" WHERE project_id = :pid '
                    f"ON CONFLICT DO NOTHING",
                ),
                {"pid": pid},
            )
            conn.execute(
                text(f'DELETE FROM "{top_default}" WHERE project_id = :pid'),
                {"pid": pid},
            )
            conn.execute(
                text(
                    f'ALTER TABLE "{table}" ATTACH PARTITION "{sub_parent}" '
                    f"FOR VALUES IN ({pid})",
                ),
            )
        created.append(sub_parent)

    if created:
        with engine.begin() as conn:
            tune_partition_storage(conn)
            for partition in created:
                conn.execute(text(f'ANALYZE "{partition}"'))
    return created


def promote_owner_online(
    engine: Engine,
    project_id: int,
    owner_key: str,
    *,
    batch_size: int = 50_000,
) -> list[str]:
    """Online (non-locking) equivalent of :func:`promote_owner`.

    Carves one owner out of an owner-sub-partitioned project's sub-DEFAULT into
    its own sub-partition without a long ATTACH-time index build under lock.
    Idempotent and resumable. Returns the sub-partitions attached by this call.
    """
    pid = int(project_id)
    created: list[str] = []
    for table in OWNER_SUB_TABLES:
        sub_parent = dedicated_partition_name(table, pid)
        partition = owner_subpartition_name(table, pid, owner_key)
        if _online_promote_leaf(
            engine,
            parent=sub_parent,
            partition=partition,
            for_values_sql=f"FOR VALUES IN ('{owner_key}')",
            source_partition=f"{sub_parent}_default",
            where_sql="owner_key = :ok",
            where_params={"ok": owner_key},
            id_col=_id_col_for(table),
            batch_size=batch_size,
        ):
            created.append(partition)
    if created:
        with engine.begin() as conn:
            tune_partition_storage(conn)
            for partition in created:
                conn.execute(text(f'ANALYZE "{partition}"'))
    return created
