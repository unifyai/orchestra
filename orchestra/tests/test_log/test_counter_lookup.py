"""Correctness tests for materialized context counter lookup state."""

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, List, Tuple

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
        log_event = LogEvent(project_id=project.id, data=row)
        session.add(log_event)
        session.flush()
        session.add(
            LogEventContext(
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
    new_log = LogEvent(project_id=project.id, data={})
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
