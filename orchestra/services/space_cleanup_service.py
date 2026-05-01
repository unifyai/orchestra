"""Cleanup orchestration for shared-space lifecycle operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.space_dao import SPACE_STATUS_ACTIVE, SPACE_STATUS_DELETING
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    Assistant,
    AssistantSpaceMembership,
    ContactMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Space,
    SpaceInvite,
)
from orchestra.services import task_machine_state_service
from orchestra.web.api.utils.assistant_infra import (
    ADMIN_KEY,
    _comms_url_for,
    reawaken_assistant,
)
from orchestra.web.api.utils.http_client import get_async_client

TASK_ACTIVATION_DELETE_PATH = "/infra/task-activation/delete"
TASK_ACTIVATION_DELETE_TIMEOUT_SECONDS = 20.0
POSTGRES_LOCK_NOT_AVAILABLE = "55P03"


class SpaceCleanupNotFoundError(Exception):
    """Raised when a space cleanup target no longer exists."""


class SpaceCleanupAuthError(Exception):
    """Raised when a caller cannot administer a space cleanup target."""


class SpaceCleanupConflictError(Exception):
    """Raised when a cleanup cannot acquire the required row lock."""


@dataclass(slots=True)
class SpaceCleanupFailure(Exception):
    """A retryable cleanup failure with the phase that failed."""

    phase: int
    reason: str


def _space_destination(space_id: int) -> str:
    """Return the activation destination string for one space."""

    return f"space:{space_id}"


def _assert_space_mutation_allowed(
    session: Session,
    *,
    user_id: str,
    space: Space,
) -> None:
    """Require user ownership or organization write access for a space."""

    if space.owner_user_id == user_id:
        return
    if space.organization_id is None:
        raise SpaceCleanupAuthError("space_mutation_forbidden")

    has_permission = ResourceAccessDAO(session).check_org_member_permission(
        user_id,
        space.organization_id,
        "org:write",
    )
    if not has_permission:
        raise SpaceCleanupAuthError("space_mutation_forbidden")


def _lock_space_for_cleanup(
    session: Session,
    *,
    space_id: int,
    user_id: str,
) -> Space:
    """Lock a space row and mark it as deleting."""

    try:
        space = session.execute(
            select(Space)
            .where(Space.space_id == space_id)
            .with_for_update(nowait=True),
        ).scalar_one_or_none()
    except OperationalError as exc:
        if getattr(exc.orig, "pgcode", None) == POSTGRES_LOCK_NOT_AVAILABLE:
            raise SpaceCleanupConflictError("space_cleanup_lock_unavailable") from exc
        raise

    if space is None:
        raise SpaceCleanupNotFoundError("space_not_found")
    _assert_space_mutation_allowed(session, user_id=user_id, space=space)
    if space.status == SPACE_STATUS_ACTIVE:
        space.status = SPACE_STATUS_DELETING
    session.flush()
    return space


def _member_assistant_ids(session: Session, *, space_id: int) -> list[int]:
    """Return assistant ids that currently belong to a space."""

    return [
        int(assistant_id)
        for (assistant_id,) in session.execute(
            select(AssistantSpaceMembership.assistant_id)
            .where(AssistantSpaceMembership.space_id == space_id)
            .order_by(AssistantSpaceMembership.assistant_id.asc()),
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


def _scheduled_activations_for_space(
    session: Session,
    *,
    space_id: int,
    assistant_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return projected scheduled activations targeting a space."""

    project_ids = _assistants_project_ids(session)
    if not project_ids:
        return []

    destination = _space_destination(space_id)
    query = (
        select(LogEvent.data)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .where(
            LogEvent.project_id.in_(project_ids),
            Context.project_id.in_(project_ids),
            Context.name.like(
                f"%/{task_machine_state_service.TASK_ACTIVATIONS_CONTEXT_NAME}",
            ),
            LogEvent.data["destination"].astext == destination,
        )
        .order_by(LogEvent.id.asc())
    )
    if assistant_id is not None:
        query = query.where(LogEvent.data["assistant_id"].astext == str(assistant_id))

    return [dict(data or {}) for (data,) in session.execute(query).all()]


async def _delete_scheduled_activation(activation: Mapping[str, Any]) -> None:
    """Delete one scheduled activation through the Communication admin API."""

    body = task_machine_state_service._scheduled_activation_delete_body(activation)
    if body is None:
        return

    comms_url = _comms_url_for().rstrip("/")
    if not comms_url or not ADMIN_KEY:
        raise RuntimeError("Communication admin endpoint is not configured")

    client = get_async_client()
    response = await client.request(
        "POST",
        f"{comms_url}{TASK_ACTIVATION_DELETE_PATH}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json=body,
        timeout=TASK_ACTIVATION_DELETE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


async def _revoke_space_activations(
    session: Session,
    *,
    space_id: int,
    assistant_id: int | None = None,
) -> None:
    """Revoke every scheduled activation targeting a space or member pair."""

    for activation in _scheduled_activations_for_space(
        session,
        space_id=space_id,
        assistant_id=assistant_id,
    ):
        await _delete_scheduled_activation(activation)


def _shared_space_contexts(session: Session, *, space_id: int) -> list[Context]:
    """Return shared contexts rooted at one space."""

    project_ids = _assistants_project_ids(session)
    if not project_ids:
        return []

    prefix = f"Spaces/{space_id}"
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


def _purge_shared_space_contexts(session: Session, *, space_id: int) -> None:
    """Delete shared context roots for a space through the context DAO."""

    context_dao = ContextDAO(session)
    for context in _shared_space_contexts(session, space_id=space_id):
        context_dao.delete(context.id, commit=False)


def _drop_space_rows(session: Session, *, space_id: int) -> None:
    """Delete relational rows owned by a space."""

    session.execute(delete(SpaceInvite).where(SpaceInvite.space_id == space_id))
    session.execute(
        delete(AssistantSpaceMembership).where(
            AssistantSpaceMembership.space_id == space_id,
        ),
    )
    session.execute(delete(Space).where(Space.space_id == space_id))
    session.flush()


def _drop_contact_memberships_for_space(
    session: Session,
    *,
    assistant_id: int,
    space_id: int,
) -> None:
    """Delete assistant-owned contact metadata scoped to one space."""

    session.execute(
        delete(ContactMembership).where(
            ContactMembership.assistant_id == assistant_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == space_id,
        ),
    )
    session.flush()


async def delete_space(session: Session, *, space_id: int, user_id: str) -> None:
    """Delete a space through the ordered shared-space cascade."""

    _lock_space_for_cleanup(session, space_id=space_id, user_id=user_id)
    member_assistant_ids = _member_assistant_ids(session, space_id=space_id)
    session.commit()

    try:
        await _revoke_space_activations(session, space_id=space_id)
    except Exception as exc:
        raise SpaceCleanupFailure(phase=2, reason=str(exc)) from exc

    try:
        for assistant_id in member_assistant_ids:
            await purge_assistant_overlay(
                session,
                assistant_id=assistant_id,
                space_id=space_id,
                revoke_activations=False,
                remove_membership=False,
            )
        _purge_shared_space_contexts(session, space_id=space_id)
        session.commit()
    except SpaceCleanupFailure:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise SpaceCleanupFailure(phase=3, reason=str(exc)) from exc

    try:
        _drop_space_rows(session, space_id=space_id)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise SpaceCleanupFailure(phase=4, reason=str(exc)) from exc


async def purge_assistant_overlay(
    session: Session,
    *,
    assistant_id: int,
    space_id: int,
    revoke_activations: bool = True,
    remove_membership: bool = True,
) -> None:
    """Remove assistant-owned state for one space membership.

    The overlay portion is limited to membership-scoped contact metadata.
    Space-owned shared data, including Secrets rows under `Spaces/{id}`, is
    handled by whole-space context deletion instead.
    """

    if revoke_activations:
        try:
            await _revoke_space_activations(
                session,
                space_id=space_id,
                assistant_id=assistant_id,
            )
        except Exception as exc:
            raise SpaceCleanupFailure(phase=2, reason=str(exc)) from exc

    _drop_contact_memberships_for_space(
        session,
        assistant_id=assistant_id,
        space_id=space_id,
    )

    if remove_membership:
        session.execute(
            delete(AssistantSpaceMembership).where(
                AssistantSpaceMembership.assistant_id == assistant_id,
                AssistantSpaceMembership.space_id == space_id,
            ),
        )
        session.flush()


async def purge_assistant_memberships(
    session: Session,
    *,
    assistant: Assistant,
) -> None:
    """Remove every space membership for an assistant before row deletion."""

    membership_space_ids = [
        int(space_id)
        for (space_id,) in session.execute(
            select(AssistantSpaceMembership.space_id)
            .where(AssistantSpaceMembership.assistant_id == assistant.agent_id)
            .order_by(AssistantSpaceMembership.space_id.asc()),
        ).all()
    ]
    for space_id in membership_space_ids:
        await purge_assistant_overlay(
            session,
            assistant_id=assistant.agent_id,
            space_id=space_id,
        )

    if membership_space_ids:
        await reawaken_assistant(
            str(assistant.agent_id),
            deploy_env=assistant.deploy_env,
            data={
                "assistant_id": str(assistant.agent_id),
                "space_ids": json.dumps([]),
                "update_kind": "membership",
            },
        )
