"""Resolve a body's self/boss contact ids from its ``ContactMembership`` overlay.

The shared ``Contacts`` table carries one row per contact (Hive-shared for Hive
bodies, per-body for solo bodies). Per-body state — including the relationship
label that marks a row as "self" or "boss" — lives on a separate overlay
context: ``{user_id}/{assistant_id}/ContactMembership``. Each overlay row holds
``{contact_id, relationship, should_respond, response_policy, can_edit}`` as the
log event's ``data`` blob.

This module exposes a single helper that loads the overlay rows for one body
and returns the ``contact_id`` of the latest ``relationship == "self"`` and
``relationship == "boss"`` rows, both as ``int | None``. Callers that need this
information on ``AssistantRead`` route through ``resolve_membership_contact_ids``
so the derivation stays in one place.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    _coerce_int,
)

CONTACT_MEMBERSHIP_TABLE = "ContactMembership"
RELATIONSHIP_SELF = "self"
RELATIONSHIP_BOSS = "boss"


def _membership_context_name(user_id: str, assistant_id: int) -> str:
    """Return the per-body ``ContactMembership`` overlay context name."""

    return f"{user_id}/{assistant_id}/{CONTACT_MEMBERSHIP_TABLE}"


def _reduce_membership_rows(
    data_rows: Iterable[object],
) -> Tuple[Optional[int], Optional[int]]:
    """Reduce ``ContactMembership`` log-event ``data`` blobs to latest self/boss ids.

    ``data_rows`` must already be ordered oldest-first; the most recent row
    per relationship wins. Non-dict rows and rows missing a valid integer
    ``contact_id`` are skipped.
    """

    self_contact_id: Optional[int] = None
    boss_contact_id: Optional[int] = None
    for data in data_rows:
        if not isinstance(data, dict):
            continue
        contact_id = _coerce_int(data.get("contact_id"))
        if contact_id is None:
            continue
        relationship = data.get("relationship")
        if relationship == RELATIONSHIP_SELF:
            self_contact_id = contact_id
        elif relationship == RELATIONSHIP_BOSS:
            boss_contact_id = contact_id
    return self_contact_id, boss_contact_id


def resolve_membership_contact_ids(
    session: Session,
    assistant: Assistant,
) -> Tuple[Optional[int], Optional[int]]:
    """Return ``(self_contact_id, boss_contact_id)`` for one body.

    Both values are ``None`` when the body has no ``ContactMembership`` overlay
    rows yet (for example, a freshly-provisioned body whose setup eager
    materialization has not run). Individual values are ``None`` when no row
    carries the matching ``relationship``. The most recent row per relationship
    wins if multiple rows exist.

    The overlay context is resolved inside the ``Assistants`` project. Projects
    are org-scoped when the assistant belongs to an organization and
    user-scoped otherwise, matching how bodies write their own logs.
    """

    membership_ctx = _membership_context_name(assistant.user_id, assistant.agent_id)

    project_query = select(Project.id).where(Project.name == TASK_MACHINE_PROJECT_NAME)
    if assistant.organization_id is not None:
        project_query = project_query.where(
            Project.organization_id == assistant.organization_id,
        )
    else:
        project_query = project_query.where(
            Project.user_id == assistant.user_id,
            Project.organization_id.is_(None),
        )
    project_id = session.execute(project_query).scalar_one_or_none()
    if project_id is None:
        return None, None

    rows = session.execute(
        select(LogEvent.data)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .where(Context.project_id == project_id)
        .where(Context.name == membership_ctx)
        .order_by(LogEvent.created_at.asc(), LogEvent.id.asc()),
    ).all()

    return _reduce_membership_rows(data for (data,) in rows)


def resolve_membership_contact_ids_bulk(
    session: Session,
    assistants: Iterable[Assistant],
) -> Dict[int, Tuple[Optional[int], Optional[int]]]:
    """Batch-resolve ``(self_contact_id, boss_contact_id)`` for many bodies.

    Returns a ``dict`` keyed by ``Assistant.agent_id`` with
    ``(self, boss)`` tuples. Bodies with no ``ContactMembership`` overlay
    rows map to ``(None, None)``. All bodies are queried in at most two
    database round-trips (one to look up the ``Assistants`` project ids
    scoped to each org/user, one to pull every overlay row across bodies),
    avoiding the N+1 pattern that a per-body
    :func:`resolve_membership_contact_ids` call would incur when wired
    into a list-endpoint comprehension.
    """

    assistant_list = list(assistants)
    result: Dict[int, Tuple[Optional[int], Optional[int]]] = {
        int(a.agent_id): (None, None) for a in assistant_list
    }
    if not assistant_list:
        return result

    # One Project row per distinct (scope_kind, scope_id) pair. Scope
    # kinds match how bodies write their own logs: org-scoped assistants
    # use the shared org Project; non-org assistants use their owner's
    # personal Project.
    org_ids: set[str] = set()
    user_ids: set[str] = set()
    for a in assistant_list:
        if a.organization_id is not None:
            org_ids.add(str(a.organization_id))
        else:
            user_ids.add(str(a.user_id))

    project_filters = []
    if org_ids:
        project_filters.append(
            and_(
                Project.organization_id.in_(org_ids),
            ),
        )
    if user_ids:
        project_filters.append(
            and_(
                Project.user_id.in_(user_ids),
                Project.organization_id.is_(None),
            ),
        )
    if not project_filters:
        return result

    project_rows = session.execute(
        select(Project.id, Project.organization_id, Project.user_id)
        .where(Project.name == TASK_MACHINE_PROJECT_NAME)
        .where(or_(*project_filters)),
    ).all()

    org_project_id: Dict[str, int] = {}
    user_project_id: Dict[str, int] = {}
    for project_id, organization_id, user_id in project_rows:
        if organization_id is not None:
            org_project_id[str(organization_id)] = int(project_id)
        else:
            user_project_id[str(user_id)] = int(project_id)

    # Map each body's expected (project_id, context_name) back to its
    # agent_id so a single JOINed query can carry every overlay row we
    # need in one shot.
    key_to_agent_id: Dict[Tuple[int, str], int] = {}
    project_id_to_ctx_names: Dict[int, List[str]] = defaultdict(list)
    for a in assistant_list:
        if a.organization_id is not None:
            pid = org_project_id.get(str(a.organization_id))
        else:
            pid = user_project_id.get(str(a.user_id))
        if pid is None:
            continue
        ctx_name = _membership_context_name(a.user_id, a.agent_id)
        key_to_agent_id[(pid, ctx_name)] = int(a.agent_id)
        project_id_to_ctx_names[pid].append(ctx_name)

    if not key_to_agent_id:
        return result

    # Per-project IN filter on context names keeps the plan tight on
    # large orgs where many bodies share the same project id.
    overlay_rows = session.execute(
        select(
            Context.project_id,
            Context.name,
            LogEvent.data,
            LogEvent.created_at,
            LogEvent.id,
        )
        .join(LogEventContext, LogEventContext.context_id == Context.id)
        .join(LogEvent, LogEvent.id == LogEventContext.log_event_id)
        .where(
            or_(
                *[
                    and_(
                        Context.project_id == pid,
                        Context.name.in_(ctx_names),
                    )
                    for pid, ctx_names in project_id_to_ctx_names.items()
                ],
            ),
        )
        .order_by(LogEvent.created_at.asc(), LogEvent.id.asc()),
    ).all()

    per_agent: Dict[int, List[object]] = defaultdict(list)
    for project_id, ctx_name, data, _created_at, _ev_id in overlay_rows:
        agent_id = key_to_agent_id.get((int(project_id), ctx_name))
        if agent_id is None:
            continue
        per_agent[agent_id].append(data)

    for agent_id, rows in per_agent.items():
        result[agent_id] = _reduce_membership_rows(rows)

    return result
