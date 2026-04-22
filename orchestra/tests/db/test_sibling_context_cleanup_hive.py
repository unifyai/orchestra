"""Hive-awareness tests for ``sibling_context_cleanup``.

The sibling-context cleanup helper derives tier-1/2/3 aggregate candidates
from the ``{user}/{assistant}/All/{sub}`` shape used under per-body trees.
Hive-scoped rows live in a ``Hives/{hive_id}/...`` tree that has no ``All/``
aggregate segment, so the helper must short-circuit on the Hive prefix before
the tier derivation can fabricate nonsense candidates.

These tests exercise the real
:func:`orchestra.db.dao.sibling_context_cleanup.get_assistants_sibling_context_info`
function against the test database, asserting both the Hive short-circuit and
that the solo per-body path still resolves Tier 1 siblings end to end.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.sibling_context_cleanup import get_assistants_sibling_context_info
from orchestra.db.models.orchestra_models import LogEvent, LogEventContext, Project


def _ensure_project(dbsession: Session, name: str) -> Project:
    """Return an existing ``name``-named project or create one on the fly."""

    project = dbsession.query(Project).filter(Project.name == name).first()
    if project is not None:
        return project
    project = Project(name=name, user_id="user1")
    dbsession.add(project)
    dbsession.flush()
    return project


def _create_log_in_context(
    dbsession: Session,
    *,
    project_id: int,
    context_id: int,
    data: dict,
) -> int:
    """Insert a ``LogEvent`` and wire it into ``context_id``; return the log id."""

    log = LogEvent(project_id=project_id, data=data)
    dbsession.add(log)
    dbsession.flush()
    dbsession.add(LogEventContext(log_event_id=log.id, context_id=context_id))
    dbsession.flush()
    return log.id


# --------------------------------------------------------------------------- #
# Hive short-circuit
# --------------------------------------------------------------------------- #


def test_hive_path_short_circuits_without_touching_tier_logic(dbsession: Session):
    """``Hives/{h}/...`` context names must return an empty sibling map."""

    project = _ensure_project(dbsession, "Assistants")
    context_dao = ContextDAO(dbsession)
    hive_context_id = context_dao.get_or_create(
        project_id=project.id,
        name="Hives/42/Contacts",
    )
    log_id = _create_log_in_context(
        dbsession,
        project_id=project.id,
        context_id=hive_context_id,
        data={"_assistant_id": "7"},
    )

    sibling_map = get_assistants_sibling_context_info(
        session=dbsession,
        project_id=project.id,
        context_id=hive_context_id,
        context_name="Hives/42/Contacts",
        log_event_ids=[log_id],
        context_dao=context_dao,
    )

    assert sibling_map == {}


def test_pathological_hive_all_path_is_also_refused(dbsession: Session):
    """A rogue ``Hives/{h}/All/...`` name from a Hive-ignorant writer is rejected.

    Without the Hive guard the tier logic would happily parse ``All`` out of
    the middle of the path and fabricate cross-tier candidate siblings that
    have nothing to do with per-body aggregation. Short-circuiting on the
    Hive prefix catches the pathological shape up front.
    """

    project = _ensure_project(dbsession, "Assistants")
    context_dao = ContextDAO(dbsession)
    hive_all_context_id = context_dao.get_or_create(
        project_id=project.id,
        name="Hives/42/All/Contacts",
    )
    log_id = _create_log_in_context(
        dbsession,
        project_id=project.id,
        context_id=hive_all_context_id,
        data={"_user": "user1", "_assistant": "assistant1"},
    )

    sibling_map = get_assistants_sibling_context_info(
        session=dbsession,
        project_id=project.id,
        context_id=hive_all_context_id,
        context_name="Hives/42/All/Contacts",
        log_event_ids=[log_id],
        context_dao=context_dao,
    )

    assert sibling_map == {}


# --------------------------------------------------------------------------- #
# Solo per-body regression
# --------------------------------------------------------------------------- #


def test_solo_per_body_path_still_cascades_to_tier2_and_tier3(dbsession: Session):
    """Deleting from the archive must still fan out to per-user and per-body siblings.

    Archive protection shields the ``All/*`` tier when deletes originate below
    it. To exercise the full tier-1/2/3 derivation we originate the delete from
    the archive itself and assert both lower tiers are returned as siblings.
    """

    project = _ensure_project(dbsession, "Assistants")
    context_dao = ContextDAO(dbsession)

    tier1_context_id = context_dao.get_or_create(
        project_id=project.id,
        name="All/Metrics",
    )
    tier2_context_id = context_dao.get_or_create(
        project_id=project.id,
        name="user1/All/Metrics",
    )
    tier3_context_id = context_dao.get_or_create(
        project_id=project.id,
        name="user1/assistant1/Metrics",
    )

    log_id = _create_log_in_context(
        dbsession,
        project_id=project.id,
        context_id=tier1_context_id,
        data={"_user": "user1", "_assistant": "assistant1", "value": 1},
    )
    dbsession.add(LogEventContext(log_event_id=log_id, context_id=tier2_context_id))
    dbsession.add(LogEventContext(log_event_id=log_id, context_id=tier3_context_id))
    dbsession.flush()

    sibling_map = get_assistants_sibling_context_info(
        session=dbsession,
        project_id=project.id,
        context_id=tier1_context_id,
        context_name="All/Metrics",
        log_event_ids=[log_id],
        context_dao=context_dao,
    )

    assert log_id in sibling_map
    assert set(sibling_map[log_id]) == {tier2_context_id, tier3_context_id}
