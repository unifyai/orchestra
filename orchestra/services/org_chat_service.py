"""Org chat: team group-chat persistence and hosted dispatch.

Team group-chat messages are stored in the log-backed
``Teams/{team_id}/GroupChat`` context of the organization's ``Assistants``
project. That placement is deliberate:

* ``owner_scope='team'`` means the thread is purged with the rest of the
  team's shared memory on team deletion (no extra cleanup path), and
* team assistants can read the thread as ordinary shared team data.

Sender identity is stored explicitly on each row (``sender_kind`` +
``sender_user_id`` / ``sender_assistant_id`` + ``sender_name``) rather than
via per-assistant contact ids, because contact ids are scoped to one
assistant and are ambiguous in a multi-party room.

Realtime delivery and assistant fan-out are delegated to the hosted
communication layer (adapters ``POST /unify/org-chat``): one publish to the
per-organization Pub/Sub topic for Console SSE, plus one standard
``unify_message`` envelope per non-coordinator team assistant — team chat is
ordinary unify_message traffic fanned out to every team assistant, like a
large email CC chain. Assistant-authored messages fan out the same way
(excluding the author), so AI replies are part of every teammate's
conversational context; whether to respond is each receiving brain's normal
judgement, as on any other medium.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Team,
    User,
)
from orchestra.db.scope import resolve_owner, single_owner_key
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal

logger = logging.getLogger(__name__)

ASSISTANTS_PROJECT_NAME = "Assistants"
GROUP_CHAT_CONTEXT_SUFFIX = "GroupChat"
GROUP_CHAT_UNIQUE_KEYS = {"message_id": "int"}
GROUP_CHAT_AUTO_COUNTING = {"message_id": None}

SENDER_KIND_USER = "user"
SENDER_KIND_ASSISTANT = "assistant"


def group_chat_context_name(team_id: int) -> str:
    return f"Teams/{team_id}/{GROUP_CHAT_CONTEXT_SUFFIX}"


def _resolve_org_assistants_project(
    session: Session,
    *,
    organization_id: int,
) -> Project:
    project = session.scalar(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.name == ASSISTANTS_PROJECT_NAME,
        ),
    )
    if project is None:
        raise ValueError(
            "Assistants project is required for team group chat "
            f"(organization={organization_id}).",
        )
    return project


def _ensure_group_chat_context(
    session: Session,
    *,
    project_id: int,
    team_id: int,
) -> Context:
    context_name = group_chat_context_name(team_id)
    context = session.scalar(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == context_name,
        ),
    )
    if context is not None:
        return context

    owner_scope, owner_id = resolve_owner(context_name)
    context = Context(
        project_id=project_id,
        name=context_name,
        is_versioned=False,
        allow_duplicates=True,
        unique_key_names=list(GROUP_CHAT_UNIQUE_KEYS.keys()),
        unique_key_types=list(GROUP_CHAT_UNIQUE_KEYS.values()),
        auto_counting=GROUP_CHAT_AUTO_COUNTING,
        owner_scope=owner_scope,
        owner_id=owner_id,
    )
    session.add(context)
    session.flush()
    return context


def _build_project_dao(session: Session) -> ProjectDAO:
    return ProjectDAO(
        session,
        organization_member_dao=OrganizationMemberDAO(session),
        context_dao=ContextDAO(session),
    )


def persist_team_message(
    session: Session,
    *,
    team: Team,
    sender_kind: str,
    sender_user_id: str | None,
    sender_assistant_id: int | None,
    sender_name: str,
    content: str,
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append one message to the team's GroupChat thread.

    Returns the stored message payload including the auto-assigned
    ``message_id`` and timestamp.
    """
    project = _resolve_org_assistants_project(
        session,
        organization_id=team.organization_id,
    )
    context = _ensure_group_chat_context(
        session,
        project_id=project.id,
        team_id=team.id,
    )
    entries = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender_kind": sender_kind,
        "sender_name": sender_name,
        "content": content,
        "mentions": mentions or [],
        "attachments": attachments or [],
    }
    # None-valued fields are omitted so field types are always inferred from
    # real values (sender_user_id for humans, sender_assistant_id for AIs).
    if sender_user_id is not None:
        entries["sender_user_id"] = sender_user_id
    if sender_assistant_id is not None:
        entries["sender_assistant_id"] = sender_assistant_id
    result = create_logs_internal(
        request=CreateLogConfig(
            project_name=ASSISTANTS_PROJECT_NAME,
            context=context.name,
            entries=entries,
        ),
        project_id=project.id,
        context_id=context.id,
        project_dao=_build_project_dao(session),
        field_type_dao=FieldTypeDAO(session),
        log_event_dao=LogEventDAO(session),
        context_dao=ContextDAO(session),
        context_obj=context,
    )
    if result.get("failed"):
        first_error = result["failed"][0].get("error", "Message creation failed")
        raise ValueError(str(first_error))
    session.flush()

    log_event_id = result["log_event_ids"][0]
    stored = session.scalar(
        select(LogEvent).where(
            LogEvent.project_id == project.id,
            LogEvent.id == log_event_id,
        ),
    )
    payload = dict(stored.data)
    payload["team_id"] = team.id
    payload["organization_id"] = team.organization_id
    return payload


def list_team_messages(
    session: Session,
    *,
    team: Team,
    limit: int = 100,
    before_message_id: int | None = None,
) -> list[dict[str, Any]]:
    """Most-recent-last page of a team's GroupChat thread."""
    project = _resolve_org_assistants_project(
        session,
        organization_id=team.organization_id,
    )
    context = session.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == group_chat_context_name(team.id),
        ),
    )
    if context is None:
        return []

    query = (
        project_scoped_log_events(
            context.project_id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(LogEventContext.context_id == context.id)
        .order_by(LogEvent.id.desc())
        .limit(limit)
    )
    rows = session.scalars(query).all()
    messages = []
    for row in reversed(rows):
        data = dict(row.data)
        message_id = data.get("message_id")
        if before_message_id is not None and (
            not isinstance(message_id, int) or message_id >= before_message_id
        ):
            continue
        data.setdefault("mentions", [])
        data.setdefault("attachments", [])
        data["team_id"] = team.id
        data["organization_id"] = team.organization_id
        messages.append(data)
    return messages


def search_team_messages(
    session: Session,
    *,
    team: Team,
    q: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Most-recent-first content matches for a team's GroupChat thread."""
    needle = q.strip()
    if not needle:
        return []
    project = _resolve_org_assistants_project(
        session,
        organization_id=team.organization_id,
    )
    context = session.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == group_chat_context_name(team.id),
        ),
    )
    if context is None:
        return []

    query = (
        project_scoped_log_events(
            context.project_id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data["content"].astext.ilike(f"%{needle}%"),
        )
        .order_by(LogEvent.id.desc())
        .limit(limit)
    )
    rows = session.scalars(query).all()
    matches = []
    for row in rows:
        data = dict(row.data)
        data.setdefault("mentions", [])
        data.setdefault("attachments", [])
        data["team_id"] = team.id
        data["organization_id"] = team.organization_id
        matches.append(data)
    return matches


def team_chat_participants(
    session: Session,
    *,
    team: Team,
) -> dict[str, list[dict[str, Any]]]:
    """Humans and non-coordinator assistants participating in a team chat."""
    team_dao = TeamDAO(session)

    member_user_ids = team_dao.get_team_members(team.id)
    users = (
        session.query(User).filter(User.id.in_(member_user_ids)).all()
        if member_user_ids
        else []
    )
    humans = [
        {
            "user_id": user.id,
            "name": " ".join(part for part in [user.name, user.last_name] if part)
            or user.email,
        }
        for user in users
    ]

    assistants = [
        {
            "assistant_id": assistant.agent_id,
            "name": " ".join(
                part for part in [assistant.first_name, assistant.surname] if part
            )
            or f"Assistant {assistant.agent_id}",
        }
        for _, assistant in team_dao.list_assistant_members(team.id)
        if not assistant.is_coordinator
    ]
    return {"humans": humans, "assistants": assistants}


def assistant_email(session: Session, assistant_id: int) -> str:
    """The assistant's provisioned email address ("" when none).

    Team-chat fan-out carries this as the sender identity for AI-authored
    messages so receiving runtimes can resolve (or provision) a teammate
    contact for the author.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    for contact in AssistantContactDAO(session).get_active_contacts_for_assistant(
        assistant_id,
    ):
        if contact.contact_type == "email" and contact.contact_value:
            return contact.contact_value
    return ""


def build_team_dispatch_payload(
    session: Session,
    *,
    team: Team,
    message: dict[str, Any],
    sender_email: str = "",
    exclude_assistant_id: int | None = None,
) -> dict[str, Any]:
    """Build the adapters ``/unify/org-chat`` payload for one team message.

    Every team message — human- or assistant-authored — fans out to every
    non-coordinator team assistant on the standard ``unify_message`` thread,
    like a large email CC chain. ``exclude_assistant_id`` skips the authoring
    assistant (it already has its own copy of what it said).

    ``sender_email`` lets each runtime resolve the sender against its own
    Contacts table when the sender is not that assistant's owner.
    """
    participants = team_chat_participants(session, team=team)
    return {
        "kind": "team",
        "organization_id": team.organization_id,
        "team_id": team.id,
        "message": message,
        "fanout_assistant_ids": [
            entry["assistant_id"]
            for entry in participants["assistants"]
            if entry["assistant_id"] != exclude_assistant_id
        ],
        "assistant_event": {
            "team_id": team.id,
            "team_name": team.name,
            "organization_id": team.organization_id,
            "body": message.get("content") or "",
            "group_message_id": message.get("message_id"),
            "sender_kind": message.get("sender_kind") or SENDER_KIND_USER,
            "sender_user_id": message.get("sender_user_id") or "",
            "sender_assistant_id": message.get("sender_assistant_id"),
            "sender_email": sender_email,
            "sender_name": message.get("sender_name") or "",
            "attachments": message.get("attachments") or [],
        },
    }
