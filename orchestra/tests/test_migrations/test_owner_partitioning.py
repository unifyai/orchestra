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

from typing import Generator

import pytest
from sqlalchemy import Engine, create_engine, text

from orchestra.db.partitioning import (
    OWNER_SUB_TABLES,
    PARTITIONED_TABLES,
    build_partitioned_index,
    dedicated_partition_name,
    drop_owner,
    find_owner_promotion_candidates,
    index_attached_to_child,
    is_partitioned,
    owner_subpartition_name,
    promote_owner,
    promote_owner_online,
    promote_project_online,
    relation_exists,
    sub_partition_project_by_owner,
    sub_partition_project_by_owner_online,
    tune_partition_storage,
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


def _leaf_reloptions(conn, qualified_leaf: str) -> dict[str, str]:
    opts = conn.execute(
        text("SELECT reloptions FROM pg_class WHERE oid = cast(:n AS regclass)"),
        {"n": qualified_leaf},
    ).scalar()
    return dict(opt.split("=", 1) for opt in (opts or []))


def test_tune_partition_storage_right_sizes_the_queue(dbsession) -> None:
    """The high-churn ``embedding_queue`` leaves get small autovacuum thresholds
    while the growing ``log_event`` leaves keep the large fixed thresholds.

    Builds scratch copies of the two families (search_path scoped to the scratch
    schema so the real public tables are invisible to the literal-name lookups
    in ``tune_partition_storage``) and inspects the applied reloptions.
    """
    conn = dbsession.connection()
    conn.execute(text("CREATE SCHEMA queue_tune_scratch"))
    conn.execute(text("SET search_path TO queue_tune_scratch"))

    conn.execute(
        text(
            "CREATE TABLE log_event (project_id int NOT NULL, id bigint NOT NULL, "
            "owner_key varchar NOT NULL, data jsonb, "
            "PRIMARY KEY (project_id, id, owner_key)) PARTITION BY LIST (project_id)",
        ),
    )
    conn.execute(text("CREATE TABLE log_event_default PARTITION OF log_event DEFAULT"))
    conn.execute(
        text(
            "CREATE TABLE embedding_queue (project_id int NOT NULL, id bigint NOT NULL, "
            "owner_key varchar NOT NULL, PRIMARY KEY (project_id, id, owner_key)) "
            "PARTITION BY LIST (project_id)",
        ),
    )
    conn.execute(
        text(
            "CREATE TABLE embedding_queue_default PARTITION OF embedding_queue DEFAULT",
        ),
    )

    tune_partition_storage(conn)

    queue = _leaf_reloptions(conn, "queue_tune_scratch.embedding_queue_default")
    log_event = _leaf_reloptions(conn, "queue_tune_scratch.log_event_default")

    assert queue["autovacuum_vacuum_threshold"] == "1000"
    assert queue["autovacuum_analyze_threshold"] == "1000"
    assert queue["autovacuum_vacuum_scale_factor"] == "0.02"
    # The large log partitions keep the size-independent fixed thresholds.
    assert log_event["autovacuum_vacuum_threshold"] == "50000"
    assert log_event["autovacuum_vacuum_scale_factor"] == "0"


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


# --------------------------------------------------------------------------- #
# Online (non-locking) promotion against the real partitioned kernel tables.
#
# The promote_*_online helpers run several autonomous transactions and take a
# LOCK TABLE during cutover, so they need a normal (transactional) engine rather
# than the AUTOCOMMIT ``_engine`` fixture. They commit, so they run on the
# function-scoped test DB (dropped on teardown) instead of the rolled-back
# dbsession. Only ``project`` + ``log_event`` are seeded (satisfying the kernel
# FK to ``project``); the other kernel tables promote empty, which still
# exercises the full create/backfill/index/cutover/attach path per table.
# --------------------------------------------------------------------------- #
ONLINE_PID = 9_901_234


@pytest.fixture
def online_engine(_engine: Engine) -> Generator[Engine, None, None]:
    engine = create_engine(_engine.url, isolation_level="READ COMMITTED")
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_project_logs(engine: Engine, pid: int, owners: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO project (id, name) VALUES (:p, 'online-promote-test')"),
            {"p": pid},
        )
        for i, owner_key in enumerate(owners, start=1):
            conn.execute(
                text(
                    "INSERT INTO log_event (project_id, id, owner_key, data) "
                    "VALUES (:p, :i, :o, '{}'::jsonb)",
                ),
                {"p": pid, "i": i, "o": owner_key},
            )


def _project_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM log_event WHERE project_id = :p"),
            {"p": ONLINE_PID},
        ).scalar()


def _owner_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            ok: conn.execute(
                text(
                    "SELECT count(*) FROM log_event "
                    "WHERE project_id = :p AND owner_key = :o",
                ),
                {"p": ONLINE_PID, "o": ok},
            ).scalar()
            for ok in ("a1", "a2", "sys")
        }


def test_promote_project_online(online_engine: Engine) -> None:
    """Online project promotion moves rows into a dedicated partition, no lock.

    Asserts the rows are preserved and served from the new dedicated partition
    (the DEFAULT no longer holds them), every kernel table gets an attached
    partition, the heavy secondary indexes attached (no in-lock rebuild), and a
    re-run is an idempotent no-op.
    """
    owners = ["a1", "a1", "a2", "sys"]
    _seed_project_logs(online_engine, ONLINE_PID, owners)

    with online_engine.connect() as conn:
        assert not relation_exists(
            conn,
            dedicated_partition_name("log_event", ONLINE_PID),
        )
    assert _project_count(online_engine) == len(owners)

    created = promote_project_online(online_engine, ONLINE_PID)
    assert dedicated_partition_name("log_event", ONLINE_PID) in created

    leaf = dedicated_partition_name("log_event", ONLINE_PID)
    with online_engine.connect() as conn:
        for table in PARTITIONED_TABLES:
            assert relation_exists(conn, dedicated_partition_name(table, ONLINE_PID))
        # Rows preserved, now in the dedicated partition; DEFAULT cleared of them.
        assert conn.execute(
            text(f'SELECT count(*) FROM "{leaf}"'),
        ).scalar() == len(owners)
        assert (
            conn.execute(
                text('SELECT count(*) FROM "log_event_default" WHERE project_id = :p'),
                {"p": ONLINE_PID},
            ).scalar()
            == 0
        )
        # The parent's heavy secondary indexes (incl. the GIN on data) are
        # attached to the new leaf, not left to rebuild under lock.
        assert len(_public_index_names(conn, leaf)) >= len(
            _public_index_names(conn, "log_event_default"),
        )
    assert _project_count(online_engine) == len(owners)

    # Idempotent: the partition is already attached, so a re-run attaches nothing.
    assert promote_project_online(online_engine, ONLINE_PID) == []


def test_online_owner_subpartition_lifecycle(online_engine: Engine) -> None:
    """Online carve + owner promote reach the same end state as the in-txn path.

    Carves the project into an owner-keyed sub-partition and promotes one owner,
    both online (offline index build + brief cutover), then verifies the promoted
    owner drops in O(1) without touching the others.
    """
    owners = ["a1", "a1", "a1", "a2", "a2", "sys"]
    _seed_project_logs(online_engine, ONLINE_PID, owners)

    # Carve online; rows are preserved and the project's partition is now itself
    # partitioned by owner_key.
    sub_partition_project_by_owner_online(online_engine, ONLINE_PID)
    sub_parent = dedicated_partition_name("log_event", ONLINE_PID)
    with online_engine.connect() as conn:
        assert relation_exists(conn, sub_parent)
        assert is_partitioned(conn, sub_parent)
        assert find_owner_promotion_candidates(conn, ONLINE_PID, 3) == [("a1", 3)]
    assert _owner_counts(online_engine) == {"a1": 3, "a2": 2, "sys": 1}

    # Promote a1 online into its own sub-partition (precondition for O(1) drop).
    created = promote_owner_online(online_engine, ONLINE_PID, "a1")
    assert len(created) == len(OWNER_SUB_TABLES)
    with online_engine.connect() as conn:
        assert relation_exists(
            conn,
            owner_subpartition_name("log_event", ONLINE_PID, "a1"),
        )
    assert _owner_counts(online_engine) == {"a1": 3, "a2": 2, "sys": 1}

    # Dropping the promoted owner is an O(1) partition drop; others untouched.
    with online_engine.begin() as conn:
        assert drop_owner(conn, ONLINE_PID, "a1") == "drop_partition"
    assert _owner_counts(online_engine) == {"a1": 0, "a2": 2, "sys": 1}

    # Idempotent: the sub-parent is already attached, so a re-run is a no-op.
    assert sub_partition_project_by_owner_online(online_engine, ONLINE_PID) == []
