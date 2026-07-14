"""Correctness tests for materialized context counter lookup state."""

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import event
from sqlalchemy.orm import Session

from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.unique_constraint_dao import (
    COMPOSITE_KEY_FIELD,
    UniqueConstraintDAO,
)
from orchestra.db.models.orchestra_models import (
    Context,
    ContextCounter,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
    Project,
)


def _parent_hash(parent_values: Dict[str, Any]) -> str:
    return hashlib.md5(
        json.dumps(parent_values, sort_keys=True).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def _create_context(
    session: Session,
    *,
    unique_key_names: List[str],
    auto_counting: Dict[str, Any],
    rows: Iterable[Dict[str, Any]],
) -> Tuple[Project, Context]:
    suffix = uuid.uuid4().hex
    project = Project(name=f"counter-project-{suffix}")
    session.add(project)
    session.flush()

    context = Context(
        project_id=project.id,
        name=f"counter-context-{suffix}",
        unique_key_names=unique_key_names,
        unique_key_types=["int" for _ in unique_key_names],
        auto_counting=auto_counting,
    )
    session.add(context)
    session.flush()

    for row in rows:
        log_event = LogEvent(owner_key="sys", project_id=project.id, data=row)
        session.add(log_event)
        session.flush()
        session.add(
            LogEventContext(
                owner_key="sys",
                project_id=project.id,
                log_event_id=log_event.id,
                context_id=context.id,
            ),
        )

    session.flush()
    return project, context


def _counter(
    session: Session,
    context_id: int,
    column_name: str,
    parent_values: Dict[str, Any],
) -> ContextCounter:
    return session.get(
        ContextCounter,
        (context_id, column_name, _parent_hash(parent_values)),
    )


def test_context_counter_cold_start_bootstraps_from_existing_max(
    dbsession: Session,
):
    project, context = _create_context(
        dbsession,
        unique_key_names=["row_id"],
        auto_counting={"row_id": None},
        rows=[{"row_id": 0}, {"row_id": 2}, {"row_id": 5}],
    )

    generated = LogEventDAO(dbsession).get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"row_id": "int"},
        provided_values=[{}],
    )

    assert generated == [{"row_id": 6}]
    counter = _counter(dbsession, context.id, "row_id", {})
    assert counter is not None
    assert counter.parent_values == {}
    assert counter.next_value == 7


def test_context_counter_advances_for_multiple_values_reserved_in_one_call(
    dbsession: Session,
):
    project, context = _create_context(
        dbsession,
        unique_key_names=["row_id"],
        auto_counting={"row_id": None},
        rows=[{"row_id": 0}],
    )
    dao = LogEventDAO(dbsession)

    first_batch = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"row_id": "int"},
        provided_values=[{}, {}, {}],
    )
    second_call = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"row_id": "int"},
        provided_values=[{}],
    )

    assert first_batch == [{"row_id": 1}, {"row_id": 2}, {"row_id": 3}]
    assert second_call == [{"row_id": 4}]
    assert _counter(dbsession, context.id, "row_id", {}).next_value == 5


def test_context_counter_isolated_by_parent_values(
    dbsession: Session,
):
    project, context = _create_context(
        dbsession,
        unique_key_names=["user", "session"],
        auto_counting={"user": None, "session": "user"},
        rows=[
            {"user": 0, "session": 0},
            {"user": 0, "session": 1},
            {"user": 1, "session": 0},
        ],
    )

    generated = LogEventDAO(dbsession).get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"user": "int", "session": "int"},
        provided_values=[{"user": 0}, {"user": 1}],
    )

    assert generated == [
        {"user": 0, "session": 2},
        {"user": 1, "session": 1},
    ]
    assert _counter(dbsession, context.id, "session", {"user": 0}).next_value == 3
    assert _counter(dbsession, context.id, "session", {"user": 1}).next_value == 2


def test_context_counter_conflict_retry_advances_past_stale_value(
    dbsession: Session,
):
    project, context = _create_context(
        dbsession,
        unique_key_names=["row_id"],
        auto_counting={"row_id": None},
        rows=[{"row_id": 0}, {"row_id": 1}],
    )

    existing = (
        dbsession.query(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .filter(LogEventContext.context_id == context.id)
        .filter(LogEvent.data.op("->>")("row_id") == "1")
        .one()
    )
    dbsession.add(
        LogUniqueConstraint(
            context_id=context.id,
            project_id=project.id,
            field_name=COMPOSITE_KEY_FIELD,
            value_hash=UniqueConstraintDAO.hash_composite(
                {"row_id": 1},
                ["row_id"],
            ),
            log_event_id=existing.id,
        ),
    )
    dbsession.add(
        ContextCounter(
            context_id=context.id,
            column_name="row_id",
            parent_values_hash=_parent_hash({}),
            parent_values={},
            next_value=1,
        ),
    )
    new_log = LogEvent(owner_key="sys", project_id=project.id, data={})
    dbsession.add(new_log)
    dbsession.flush()

    generated = LogEventDAO(dbsession).get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"row_id": "int"},
        provided_values=[{}],
        log_event_ids=[new_log.id],
    )

    assert generated == [{"row_id": 2}]
    assert _counter(dbsession, context.id, "row_id", {}).next_value == 3


def _capture_counter_scans(session: Session):
    """Record executed SQL so a test can assert the counter is served from
    ``context_counter`` rather than a per-batch full-context scan.

    Returns ``(scan_statements, stop)``: ``scan_statements`` is a live list of
    statements that read the counter column out of ``log_event`` (the seed
    ``MAX(...)`` is the only legitimate one; the retired slow path scanned the
    whole context per batch). Call ``stop()`` to detach the listener.
    """
    bind = session.get_bind()
    scan_statements: List[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "nullif" in lowered and "log_event.data" in lowered:
            scan_statements.append(statement)

    event.listen(bind, "after_cursor_execute", _listener)

    def _stop() -> None:
        event.remove(bind, "after_cursor_execute", _listener)

    return scan_statements, _stop


def test_auto_counting_only_column_materializes_and_is_monotonic(
    dbsession: Session,
):
    # row_id is auto_counting but NOT a unique key — exactly the ingest-worker
    # shape (it registers a string unique key and leaves row_id auto-counting
    # only). This is the case that previously fell into the O(n^2) slow scan.
    project, context = _create_context(
        dbsession,
        unique_key_names=["ingest_key"],
        auto_counting={"row_id": None},
        rows=[
            {"ingest_key": "seed-0", "row_id": 0},
            {"ingest_key": "seed-1", "row_id": 1},
        ],
    )
    dao = LogEventDAO(dbsession)

    first = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[{"ingest_key": "a"}, {"ingest_key": "b"}],
    )
    second = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[{"ingest_key": "c"}],
    )

    assert first == [
        {"ingest_key": "a", "row_id": 2},
        {"ingest_key": "b", "row_id": 3},
    ]
    assert second == [{"ingest_key": "c", "row_id": 4}]

    counter = _counter(dbsession, context.id, "row_id", {})
    assert counter is not None
    assert counter.parent_values == {}
    assert counter.next_value == 5


def test_auto_counting_only_column_has_no_per_batch_scan(
    dbsession: Session,
):
    project, context = _create_context(
        dbsession,
        unique_key_names=["ingest_key"],
        auto_counting={"row_id": None},
        rows=[{"ingest_key": "seed", "row_id": 0}],
    )
    dao = LogEventDAO(dbsession)

    scan_statements, stop = _capture_counter_scans(dbsession)
    try:
        for i in range(4):
            dao.get_next_composite_ids(
                project_id=project.id,
                context_id=context.id,
                unique_keys={"ingest_key": "str"},
                provided_values=[{"ingest_key": f"k{i}"}],
            )
    finally:
        stop()

    # Only the one-time cold-start seed (a single MAX(...) query) may touch
    # log_event; the retired slow path scanned the whole context every batch.
    assert len(scan_statements) <= 1
    assert _counter(dbsession, context.id, "row_id", {}).next_value == 5


def test_auto_counting_only_column_reconciles_past_client_provided_value(
    dbsession: Session,
):
    # auto_counting columns can ALSO be client-assigned per entry. The counter
    # must stay ahead of provided values so server-assigned ids never collide,
    # even though an auto_counting-only column has no unique constraint to catch
    # a collision.
    project, context = _create_context(
        dbsession,
        unique_key_names=["ingest_key"],
        auto_counting={"row_id": None},
        rows=[{"ingest_key": "seed", "row_id": 0}],
    )
    dao = LogEventDAO(dbsession)

    first = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[
            {"ingest_key": "a", "row_id": 100},
            {"ingest_key": "b"},
        ],
    )
    # The provided value is preserved; the auto-assigned one is bumped past it.
    assert first[0] == {"ingest_key": "a", "row_id": 100}
    assert first[1]["ingest_key"] == "b"
    assert first[1]["row_id"] > 100

    second = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[{"ingest_key": "c"}],
    )
    # A later batch continues above the previously provided value (no collision,
    # no falling behind across batches).
    assert second[0]["row_id"] > 100
    assert _counter(dbsession, context.id, "row_id", {}).next_value > 101


def test_resync_context_counters_bumps_past_copied_values(
    dbsession: Session,
):
    # Simulate a verbatim bulk copy (e.g. context copy/clone) that inserts rows
    # carrying counter columns directly, bypassing get_next_composite_ids. The
    # materialized counter would then be behind the copied data and drift; the
    # re-sync must bump it past MAX(existing).
    project, context = _create_context(
        dbsession,
        unique_key_names=["ingest_key"],
        auto_counting={"row_id": None},
        rows=[{"ingest_key": "seed", "row_id": 0}],
    )
    dao = LogEventDAO(dbsession)

    # Seed the counter via a normal reservation (next_value -> 2).
    dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[{"ingest_key": "a"}],
    )
    assert _counter(dbsession, context.id, "row_id", {}).next_value == 2

    # Verbatim copy: insert a row with row_id far ahead, without touching the
    # counter (mirrors pg_insert(LogEvent) in the copy paths).
    copied = LogEvent(owner_key="sys", project_id=project.id, data={"row_id": 500})
    dbsession.add(copied)
    dbsession.flush()
    dbsession.add(
        LogEventContext(
            owner_key="sys",
            project_id=project.id,
            log_event_id=copied.id,
            context_id=context.id,
        ),
    )
    dbsession.flush()

    dao.resync_context_counters(context_id=context.id, project_id=project.id)
    assert _counter(dbsession, context.id, "row_id", {}).next_value == 501

    # Subsequent reservation does not collide with the copied value.
    nxt = dao.get_next_composite_ids(
        project_id=project.id,
        context_id=context.id,
        unique_keys={"ingest_key": "str"},
        provided_values=[{"ingest_key": "b"}],
    )
    assert nxt == [{"ingest_key": "b", "row_id": 501}]
