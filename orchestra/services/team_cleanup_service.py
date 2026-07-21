"""Cleanup orchestration for organization team lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.team_dao import TEAM_STATUS_ACTIVE, TEAM_STATUS_DELETING
from orchestra.db.log_queries import log_event_context_join
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Team,
    TeamAssistantMembership,
)
from orchestra.services import task_machine_state_service
from orchestra.services.team_membership_refresh_service import (
    membership_refresh_payloads,
    publish_membership_refreshes_best_effort,
)
from orchestra.web.api.utils.assistant_infra import ADMIN_KEY, _comms_url
from orchestra.web.api.utils.http_client import get_async_client

TASK_EXECUTION_DELETE_PATH = "/infra/task-execution/delete"
TASK_EXECUTION_DELETE_TIMEOUT_SECONDS = 20.0
POSTGRES_LOCK_NOT_AVAILABLE = "55P03"


class TeamCleanupNotFoundError(Exception):
    """Raised when a team cleanup target no longer exists."""


class TeamCleanupAuthError(Exception):
    """Raised when a caller cannot administer a team cleanup target."""


class TeamCleanupConflictError(Exception):
    """Raised when a cleanup cannot acquire the required row lock."""


@dataclass(slots=True)
class TeamCleanupFailure(Exception):
    """A retryable cleanup failure with the phase that failed."""

    phase: int
    reason: str


def _team_destination(team_id: int) -> str:
    """Return the activation destination string for one team."""

    return f"team:{team_id}"


def _assert_team_mutation_allowed(
    session: Session,
    *,
    user_id: str,
    team: Team,
) -> None:
    """Require organization write access for team administration."""

    has_permission = ResourceAccessDAO(session).check_org_member_permission(
        user_id,
        team.organization_id,
        "org:write",
    )
    if not has_permission:
        raise TeamCleanupAuthError("team_mutation_forbidden")


def _lock_team_for_cleanup(
    session: Session,
    *,
    team_id: int,
    user_id: str,
) -> Team:
    """Lock a team row and mark it as deleting."""

    try:
        team = session.execute(
            select(Team).where(Team.id == team_id).with_for_update(nowait=True),
        ).scalar_one_or_none()
    except OperationalError as exc:
        if getattr(exc.orig, "pgcode", None) == POSTGRES_LOCK_NOT_AVAILABLE:
            raise TeamCleanupConflictError("team_cleanup_lock_unavailable") from exc
        raise

    if team is None:
        raise TeamCleanupNotFoundError("team_not_found")
    _assert_team_mutation_allowed(session, user_id=user_id, team=team)
    if team.status == TEAM_STATUS_ACTIVE:
        team.status = TEAM_STATUS_DELETING
    session.flush()
    return team


def _member_assistant_ids(session: Session, *, team_id: int) -> list[int]:
    """Return assistant ids that currently belong to a team."""

    return [
        int(assistant_id)
        for (assistant_id,) in session.execute(
            select(TeamAssistantMembership.assistant_id)
            .where(TeamAssistantMembership.team_id == team_id)
            .order_by(TeamAssistantMembership.assistant_id.asc()),
        ).all()
    ]


def _assistants_project_ids(session: Session) -> list[int]:
    """Return all Assistants project ids that may host shared context roots."""

    return [
        int(project_id)
        for (project_id,) in session.execute(
            select(Project.id).where(
                Project.name == task_machine_state_service.TASK_MACHINE_PROJECT_NAME,
            ),
        ).all()
    ]


def _scheduled_executions_for_team(
    session: Session,
    *,
    team_id: int,
    assistant_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return projected scheduled executions targeting a team."""

    project_ids = _assistants_project_ids(session)
    if not project_ids:
        return []

    destination = _team_destination(team_id)
    query = (
        select(LogEvent.data)
        .join(LogEventContext, log_event_context_join())
        .join(Context, Context.id == LogEventContext.context_id)
        .where(
            LogEvent.project_id.in_(project_ids),
            # IN-lists do not propagate through the equijoin's equivalence class
            # the way a single = does, so pin lec directly too.
            LogEventContext.project_id.in_(project_ids),
            Context.project_id.in_(project_ids),
            Context.name.like(
                f"%/{task_machine_state_service.TASK_EXECUTIONS_CONTEXT_NAME}",
            ),
            LogEvent.data["destination"].astext == destination,
        )
        .order_by(LogEvent.id.asc())
    )
    if assistant_id is not None:
        query = query.where(LogEvent.data["assistant_id"].astext == str(assistant_id))

    return [dict(data or {}) for (data,) in session.execute(query).all()]


async def _delete_scheduled_execution(execution: Mapping[str, Any]) -> None:
    """Delete one scheduled execution through the Communication admin API."""

    body = task_machine_state_service._scheduled_execution_delete_body(execution)
    if body is None:
        return

    comms_url = _comms_url().rstrip("/")
    if not comms_url or not ADMIN_KEY:
        raise RuntimeError("Communication admin endpoint is not configured")

    client = get_async_client()
    response = await client.request(
        "POST",
        f"{comms_url}{TASK_EXECUTION_DELETE_PATH}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json=body,
        timeout=TASK_EXECUTION_DELETE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


async def _revoke_team_executions(
    session: Session,
    *,
    team_id: int,
    assistant_id: int | None = None,
) -> None:
    """Revoke every scheduled execution targeting a team or member pair."""

    for execution in _scheduled_executions_for_team(
        session,
        team_id=team_id,
        assistant_id=assistant_id,
    ):
        await _delete_scheduled_execution(execution)


def _shared_team_contexts(session: Session, *, team_id: int) -> list[Context]:
    """Return shared contexts rooted at one team."""

    project_ids = _assistants_project_ids(session)
    if not project_ids:
        return []

    prefix = f"Teams/{team_id}"
    return list(
        session.execute(
            select(Context)
            .where(
                Context.project_id.in_(project_ids),
                (Context.name == prefix) | Context.name.like(f"{prefix}/%"),
            )
            .order_by(Context.name.desc()),
        ).scalars(),
    )


def _claim_shared_team_contexts(
    session: Session,
    *,
    team_id: int,
    contexts: list[Context],
) -> None:
    """Classify legacy path-owned team contexts so owner purging removes them."""

    if not contexts:
        return

    context_ids = [int(context.id) for context in contexts]
    # Anchor the partitioned UPDATEs to the contexts' projects so they prune.
    project_ids = sorted({int(context.project_id) for context in contexts})
    team_owner_key = f"t{team_id}"
    session.execute(
        text(
            """
            UPDATE context
            SET owner_scope = 'team', owner_id = :team_id
            WHERE id = ANY(:context_ids)
            """,
        ),
        {"team_id": int(team_id), "context_ids": context_ids},
    )
    session.execute(
        text(
            """
            UPDATE log_event le
            SET owner_key = :owner_key
            FROM log_event_context lec
            WHERE lec.log_event_id = le.id
              AND lec.project_id = le.project_id
              AND le.project_id = ANY(:project_ids)
              AND lec.context_id = ANY(:context_ids)
              AND le.owner_key = 'sys'
            """,
        ),
        {
            "owner_key": team_owner_key,
            "context_ids": context_ids,
            "project_ids": project_ids,
        },
    )
    session.execute(
        text(
            """
            UPDATE log_event_context
            SET owner_key = :owner_key
            WHERE project_id = ANY(:project_ids)
              AND context_id = ANY(:context_ids)
              AND owner_key = 'sys'
            """,
        ),
        {
            "owner_key": team_owner_key,
            "context_ids": context_ids,
            "project_ids": project_ids,
        },
    )
    session.execute(
        text(
            """
            UPDATE embedding e
            SET owner_key = :owner_key
            FROM log_event_context lec
            WHERE lec.log_event_id = e.ref_id
              AND lec.project_id = e.project_id
              AND e.project_id = ANY(:project_ids)
              AND lec.context_id = ANY(:context_ids)
              AND e.owner_key = 'sys'
            """,
        ),
        {
            "owner_key": team_owner_key,
            "context_ids": context_ids,
            "project_ids": project_ids,
        },
    )
    session.flush()


def _purge_shared_team_contexts(session: Session, *, team_id: int) -> None:
    """Delete everything a team owns across its hosting projects.

    One indexed owner-scoped purge per hosting project: the team's heavy-table
    rows (owner ``t{team_id}``) and its ``Teams/{team_id}/...`` context tree are
    removed together (see :func:`orchestra.db.scope.purge_owner`) -- an O(1)
    partition drop when the team was promoted, otherwise an indexed
    owner_key-scoped delete.
    """
    from orchestra.db.scope import OwnerScope, purge_owner

    contexts = _shared_team_contexts(session, team_id=team_id)
    _claim_shared_team_contexts(session, team_id=team_id, contexts=contexts)
    for project_id in {int(c.project_id) for c in contexts}:
        purge_owner(
            session.connection(),
            project_id,
            OwnerScope.TEAM.value,
            team_id,
        )


def _drop_team_rows(session: Session, *, team_id: int) -> None:
    """Delete relational rows owned by a team."""

    session.execute(
        delete(TeamAssistantMembership).where(
            TeamAssistantMembership.team_id == team_id,
        ),
    )
    session.execute(delete(Team).where(Team.id == team_id))
    session.flush()


def _drop_contact_memberships_for_team(
    session: Session,
    *,
    assistant_id: int,
    team_id: int,
) -> None:
    """Delete assistant-owned contact metadata scoped to one team."""

    session.execute(
        delete(ContactMembership).where(
            ContactMembership.assistant_id == assistant_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM,
            ContactMembership.target_team_id == team_id,
        ),
    )
    session.flush()


async def delete_team(
    session: Session,
    *,
    team_id: int,
    user_id: str,
    organization_id: int,
) -> None:
    """Delete a team through the ordered shared-memory cascade."""

    team = session.get(Team, team_id)
    if team is None or team.organization_id != organization_id:
        raise TeamCleanupNotFoundError("team_not_found")

    # A team that owns assistants cannot be deleted out from under them:
    # their entire memory lives in this team's shared root. The owned
    # assistants must be deleted (or transferred) first.
    owned_assistant_ids = [
        row[0]
        for row in session.query(Assistant.agent_id)
        .filter(Assistant.owner_team_id == team_id)
        .all()
    ]
    if owned_assistant_ids:
        raise TeamCleanupConflictError(
            "team_owns_assistants:"
            + ",".join(str(agent_id) for agent_id in sorted(owned_assistant_ids)),
        )

    _lock_team_for_cleanup(session, team_id=team_id, user_id=user_id)
    member_assistant_ids = _member_assistant_ids(session, team_id=team_id)
    session.commit()

    try:
        await _revoke_team_executions(session, team_id=team_id)
    except Exception as exc:
        raise TeamCleanupFailure(phase=2, reason=str(exc)) from exc

    try:
        for assistant_id in member_assistant_ids:
            await purge_assistant_overlay(
                session,
                assistant_id=assistant_id,
                team_id=team_id,
                revoke_activations=False,
                remove_membership=False,
            )
        _purge_shared_team_contexts(session, team_id=team_id)
        session.commit()
    except TeamCleanupFailure:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise TeamCleanupFailure(phase=3, reason=str(exc)) from exc

    try:
        _drop_team_rows(session, team_id=team_id)
        refresh_payloads = membership_refresh_payloads(
            session,
            [
                assistant
                for assistant_id in member_assistant_ids
                if (assistant := session.get(Assistant, assistant_id)) is not None
            ],
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        raise TeamCleanupFailure(phase=4, reason=str(exc)) from exc
    await publish_membership_refreshes_best_effort(refresh_payloads)


async def purge_assistant_overlay(
    session: Session,
    *,
    assistant_id: int,
    team_id: int,
    revoke_activations: bool = True,
    remove_membership: bool = True,
) -> None:
    """Remove assistant-owned state for one team membership."""

    if revoke_activations:
        try:
            await _revoke_team_executions(
                session,
                team_id=team_id,
                assistant_id=assistant_id,
            )
        except Exception as exc:
            raise TeamCleanupFailure(phase=2, reason=str(exc)) from exc

    _drop_contact_memberships_for_team(
        session,
        assistant_id=assistant_id,
        team_id=team_id,
    )

    if remove_membership:
        session.execute(
            delete(TeamAssistantMembership).where(
                TeamAssistantMembership.assistant_id == assistant_id,
                TeamAssistantMembership.team_id == team_id,
            ),
        )
        session.flush()


async def purge_assistant_memberships(
    session: Session,
    *,
    assistant: Assistant,
) -> None:
    """Remove every team membership for an assistant before row deletion."""

    membership_team_ids = [
        int(team_id)
        for (team_id,) in session.execute(
            select(TeamAssistantMembership.team_id)
            .where(TeamAssistantMembership.assistant_id == assistant.agent_id)
            .order_by(TeamAssistantMembership.team_id.asc()),
        ).all()
    ]
    for team_id in membership_team_ids:
        await purge_assistant_overlay(
            session,
            assistant_id=assistant.agent_id,
            team_id=team_id,
        )

    if membership_team_ids:
        await publish_membership_refreshes_best_effort(
            membership_refresh_payloads(session, [assistant]),
        )
