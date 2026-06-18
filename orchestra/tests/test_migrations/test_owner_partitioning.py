"""Lifecycle test for owner sub-partitioning of the shared Assistants project.

Builds a miniature copy of the (project-partitioned, owner_key-keyed) heavy
tables in a scratch schema and exercises the helpers that phase 2 relies on:

    sub_partition_project_by_owner  ->  promote_owner  ->  drop_owner

asserting that a promoted owner is dropped in O(1) (DROP PARTITION) while an
un-promoted owner falls back to a cheap owner_key-scoped row delete, and that
neither touches the other owners' rows. Everything runs inside the dbsession
transaction, which is rolled back on teardown, so the scratch schema never
leaks into the shared test database.
"""

from __future__ import annotations

from sqlalchemy import text

from orchestra.db.partitioning import (
    OWNER_SUB_TABLES,
    build_partitioned_index,
    drop_owner,
    find_owner_promotion_candidates,
    index_attached_to_child,
    promote_owner,
    sub_partition_project_by_owner,
)

PID = 9_900_001

_DDL = {
    "log_event": (
        "project_id int NOT NULL, id bigint NOT NULL, owner_key varchar NOT NULL, "
        "data jsonb, PRIMARY KEY (project_id, id, owner_key)"
    ),
    "log_event_context": (
        "project_id int NOT NULL, log_event_id bigint NOT NULL, "
        "context_id int NOT NULL, owner_key varchar NOT NULL, "
        "PRIMARY KEY (project_id, log_event_id, context_id, owner_key)"
    ),
    "embedding": (
        "project_id int NOT NULL, id bigint NOT NULL, owner_key varchar NOT NULL, "
        "ref_id bigint, PRIMARY KEY (project_id, id, owner_key)"
    ),
}


def _counts(conn) -> dict[str, int]:
    return {
        ok: conn.execute(
            text(
                "SELECT count(*) FROM log_event "
                "WHERE project_id = :p AND owner_key = :o",
            ),
            {"p": PID, "o": ok},
        ).scalar()
        for ok in ("a1", "a2", "sys")
    }


def test_owner_partition_lifecycle(dbsession) -> None:
    conn = dbsession.connection()
    conn.execute(text("CREATE SCHEMA owner_part_scratch"))
    conn.execute(text("SET search_path TO owner_part_scratch"))

    for table, cols in _DDL.items():
        conn.execute(
            text(f"CREATE TABLE {table} ({cols}) PARTITION BY LIST (project_id)"),
        )
        conn.execute(text(f"CREATE TABLE {table}_default PARTITION OF {table} DEFAULT"))

    # Two assistants (a1, a2) plus unclassified (sys) rows in one project.
    for i, ok in enumerate(["a1", "a1", "a1", "a2", "a2", "sys"]):
        conn.execute(
            text(
                "INSERT INTO log_event (project_id, id, owner_key, data) "
                "VALUES (:p, :i, :o, '{}')",
            ),
            {"p": PID, "i": i, "o": ok},
        )
        conn.execute(
            text(
                "INSERT INTO log_event_context "
                "(project_id, log_event_id, context_id, owner_key) "
                "VALUES (:p, :i, 1, :o)",
            ),
            {"p": PID, "i": i, "o": ok},
        )
        conn.execute(
            text(
                "INSERT INTO embedding (project_id, id, owner_key, ref_id) "
                "VALUES (:p, :i, :o, :i)",
            ),
            {"p": PID, "i": i, "o": ok},
        )

    assert _counts(conn) == {"a1": 3, "a2": 2, "sys": 1}

    # Carve the project into an owner-keyed sub-partition; rows are preserved.
    sub_partition_project_by_owner(conn, PID)
    assert _counts(conn) == {"a1": 3, "a2": 2, "sys": 1}

    # Only owners over the threshold are promotion candidates; sys is excluded.
    assert find_owner_promotion_candidates(conn, PID, 3) == [("a1", 3)]
    assert find_owner_promotion_candidates(conn, PID, 2) == [("a1", 3), ("a2", 2)]

    # Promote a1 into its own sub-partition (precondition for an O(1) drop).
    created = promote_owner(conn, PID, "a1")
    assert len(created) == len(OWNER_SUB_TABLES)
    assert _counts(conn) == {"a1": 3, "a2": 2, "sys": 1}

    # Dropping a promoted owner is an O(1) partition drop; others untouched.
    assert drop_owner(conn, PID, "a1") == "drop_partition"
    assert _counts(conn) == {"a1": 0, "a2": 2, "sys": 1}

    # Dropping an un-promoted owner falls back to a scoped row delete.
    assert drop_owner(conn, PID, "a2") == "row_delete"
    assert _counts(conn) == {"a1": 0, "a2": 0, "sys": 1}


def _public_index_names(conn, table: str) -> list[str]:
    return list(
        conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
            {"t": table},
        ).scalars(),
    )


def test_build_partitioned_index_attaches_every_leaf(dbsession) -> None:
    """The lock-safe partitioned-index builder ends with a valid attached index.

    Exercises the ON ONLY parent + per-leaf + ATTACH tree logic (the
    ``CONCURRENTLY`` keyword is dropped here since the dbsession runs inside a
    transaction; the attach semantics are identical). Built in ``public`` so the
    namespace-scoped ``child_partitions`` lookup sees the leaves, then rolled
    back with the dbsession.
    """
    conn = dbsession.connection()
    parent = "_idxtest_le"
    conn.execute(
        text(
            f"CREATE TABLE {parent} (project_id int NOT NULL, id bigint NOT NULL, "
            f"owner_key varchar NOT NULL, PRIMARY KEY (project_id, id, owner_key)) "
            f"PARTITION BY LIST (project_id)",
        ),
    )
    conn.execute(text(f"CREATE TABLE {parent}_default PARTITION OF {parent} DEFAULT"))
    conn.execute(
        text(f"CREATE TABLE {parent}_p7 PARTITION OF {parent} FOR VALUES IN (7)"),
    )

    index_name = "_idxtest_project_owner"
    build_partitioned_index(
        conn,
        parent,
        index_name,
        '"project_id", "owner_key"',
        leaf_suffix="powner_idx",
        concurrently=False,
    )
    # Re-run is a no-op (idempotent / resumable).
    build_partitioned_index(
        conn,
        parent,
        index_name,
        '"project_id", "owner_key"',
        leaf_suffix="powner_idx",
        concurrently=False,
    )

    valid = conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
            "ON c.oid = i.indexrelid WHERE c.relname = :n",
        ),
        {"n": index_name},
    ).scalar()
    assert valid is True

    # Each leaf carries the parent's PK index plus exactly one attached owner
    # index (no duplicate), and both leaves are recognised as attached.
    for leaf in (f"{parent}_default", f"{parent}_p7"):
        assert len(_public_index_names(conn, leaf)) == 2
        assert index_attached_to_child(conn, index_name, leaf)
