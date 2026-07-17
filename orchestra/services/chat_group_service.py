"""Chat groups: lightweight multi-party org chats (humans + assistants).

Messages live in ``Groups/{group_id}/GroupChat`` under the org Assistants
project with ``owner_scope='group'`` so the thread is purged with the group.
Realtime delivery mirrors team chat: Console SSE via adapters ``kind=group``
plus ``unify_message`` fan-out to every non-coordinator member assistant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Assistant,
    ChatGroup,
    ChatGroupMember,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    User,
)
from orchestra.db.scope import OwnerScope, purge_owner, resolve_owner, single_owner_key
from orchestra.services.org_chat_service import (
    ASSISTANTS_PROJECT_NAME,
    GROUP_CHAT_AUTO_COUNTING,
    GROUP_CHAT_CONTEXT_SUFFIX,
    GROUP_CHAT_UNIQUE_KEYS,
    SENDER_KIND_USER,
    _build_project_dao,
    _resolve_org_assistants_project,
    apply_user_reaction,
)
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal

logger = logging.getLogger(__name__)


def chat_group_context_name(group_id: int) -> str:
    return f"Groups/{group_id}/{GROUP_CHAT_CONTEXT_SUFFIX}"


def _ensure_chat_group_context(
    session: Session,
    *,
    project_id: int,
    group_id: int,
) -> Context:
    context_name = chat_group_context_name(group_id)
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


def list_groups_for_user(
    session: Session,
    *,
    organization_id: int,
    user_id: str,
) -> list[ChatGroup]:
    return list(
        session.scalars(
            select(ChatGroup)
            .join(ChatGroupMember, ChatGroupMember.group_id == ChatGroup.id)
            .where(
                ChatGroup.organization_id == organization_id,
                ChatGroup.status == "active",
                ChatGroupMember.user_id == user_id,
            )
            .options(selectinload(ChatGroup.members))
            .order_by(ChatGroup.updated_at.desc()),
        )
        .unique()
        .all(),
    )


def get_active_group(
    session: Session,
    *,
    organization_id: int,
    group_id: int,
) -> ChatGroup | None:
    return session.scalar(
        select(ChatGroup)
        .where(
            ChatGroup.id == group_id,
            ChatGroup.organization_id == organization_id,
            ChatGroup.status == "active",
        )
        .options(selectinload(ChatGroup.members)),
    )


def is_human_group_member(
    session: Session,
    *,
    group_id: int,
    user_id: str,
) -> bool:
    return (
        session.scalar(
            select(ChatGroupMember.id).where(
                ChatGroupMember.group_id == group_id,
                ChatGroupMember.user_id == user_id,
            ),
        )
        is not None
    )


def is_assistant_group_member(
    session: Session,
    *,
    group_id: int,
    assistant_id: int,
) -> bool:
    return (
        session.scalar(
            select(ChatGroupMember.id).where(
                ChatGroupMember.group_id == group_id,
                ChatGroupMember.assistant_id == assistant_id,
            ),
        )
        is not None
    )


def _default_group_name(
    session: Session,
    *,
    user_ids: list[str],
    assistant_ids: list[int],
) -> str:
    names: list[str] = []
    if user_ids:
        users = session.scalars(select(User).where(User.id.in_(user_ids[:3]))).all()
        for user in users:
            display = " ".join(
                part for part in [user.name, user.last_name] if part
            ).strip() or (user.email or user.id)
            names.append(display)
    if len(names) < 3 and assistant_ids:
        assistants = session.scalars(
            select(Assistant).where(
                Assistant.agent_id.in_(assistant_ids[: 3 - len(names)]),
            ),
        ).all()
        for assistant in assistants:
            display = (
                " ".join(
                    part for part in [assistant.first_name, assistant.surname] if part
                ).strip()
                or f"Assistant {assistant.agent_id}"
            )
            names.append(display)
    if not names:
        return "Group"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}, {names[1]}"
    return f"{names[0]}, {names[1]} +{len(user_ids) + len(assistant_ids) - 2}"


def create_chat_group(
    session: Session,
    *,
    organization_id: int,
    created_by_user_id: str,
    name: str | None,
    user_ids: list[str],
    assistant_ids: list[int],
) -> ChatGroup:
    member_user_ids = list(dict.fromkeys([*user_ids, created_by_user_id]))
    member_assistant_ids = list(dict.fromkeys(assistant_ids))
    resolved_name = (name or "").strip() or _default_group_name(
        session,
        user_ids=[uid for uid in member_user_ids if uid != created_by_user_id],
        assistant_ids=member_assistant_ids,
    )
    group = ChatGroup(
        organization_id=organization_id,
        name=resolved_name,
        created_by_user_id=created_by_user_id,
        status="active",
    )
    session.add(group)
    session.flush()
    for user_id in member_user_ids:
        group.members.append(
            ChatGroupMember(user_id=user_id, assistant_id=None),
        )
    for assistant_id in member_assistant_ids:
        group.members.append(
            ChatGroupMember(user_id=None, assistant_id=assistant_id),
        )
    session.flush()
    return get_active_group(
        session,
        organization_id=organization_id,
        group_id=group.id,
    )


def replace_group_membership(
    session: Session,
    *,
    group: ChatGroup,
    user_ids: list[str],
    assistant_ids: list[int],
) -> ChatGroup:
    """Replace membership via the relationship collection.

    ``ChatGroup.members`` uses ``delete-orphan``; adding rows only through
    ``session.add(..., group_id=...)`` leaves them outside the collection so a
    later flush can drop them as orphans.
    """
    member_user_ids = list(dict.fromkeys([*user_ids, group.created_by_user_id]))
    member_assistant_ids = list(dict.fromkeys(assistant_ids))
    group.members.clear()
    session.flush()
    for user_id in member_user_ids:
        group.members.append(
            ChatGroupMember(user_id=user_id, assistant_id=None),
        )
    for assistant_id in member_assistant_ids:
        group.members.append(
            ChatGroupMember(user_id=None, assistant_id=assistant_id),
        )
    session.flush()
    return get_active_group(
        session,
        organization_id=group.organization_id,
        group_id=group.id,
    )


def delete_chat_group(session: Session, *, group: ChatGroup) -> None:
    """Mark deleted and purge Groups/{id}/... contexts in Assistants projects."""
    group_id = group.id
    organization_id = group.organization_id
    group.status = "deleted"
    session.flush()

    projects = session.scalars(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.name == ASSISTANTS_PROJECT_NAME,
        ),
    ).all()
    conn = session.connection()
    for project in projects:
        purge_owner(conn, project.id, OwnerScope.GROUP.value, group_id)
    session.flush()


def chat_group_participants(
    session: Session,
    *,
    group: ChatGroup,
) -> dict[str, list[dict[str, Any]]]:
    human_ids = [m.user_id for m in (group.members or []) if m.user_id]
    assistant_ids = [
        m.assistant_id for m in (group.members or []) if m.assistant_id is not None
    ]
    users = (
        session.scalars(select(User).where(User.id.in_(human_ids))).all()
        if human_ids
        else []
    )
    humans = [
        {
            "user_id": user.id,
            "name": " ".join(part for part in [user.name, user.last_name] if part)
            or user.email,
            "email": user.email,
            "image": user.image,
        }
        for user in users
    ]
    assistants_rows = (
        session.scalars(
            select(Assistant).where(Assistant.agent_id.in_(assistant_ids)),
        ).all()
        if assistant_ids
        else []
    )
    assistants = [
        {
            "assistant_id": assistant.agent_id,
            "name": " ".join(
                part for part in [assistant.first_name, assistant.surname] if part
            )
            or f"Assistant {assistant.agent_id}",
        }
        for assistant in assistants_rows
        if not assistant.is_coordinator
    ]
    return {"humans": humans, "assistants": assistants}


def persist_group_message(
    session: Session,
    *,
    group: ChatGroup,
    sender_kind: str,
    sender_user_id: str | None,
    sender_assistant_id: int | None,
    sender_name: str,
    content: str,
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = _resolve_org_assistants_project(
        session,
        organization_id=group.organization_id,
    )
    context = _ensure_chat_group_context(
        session,
        project_id=project.id,
        group_id=group.id,
    )
    entries = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender_kind": sender_kind,
        "sender_name": sender_name,
        "content": content,
        "mentions": mentions or [],
        "attachments": attachments or [],
    }
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
    payload["group_id"] = group.id
    payload["organization_id"] = group.organization_id
    return payload


def list_group_messages(
    session: Session,
    *,
    group: ChatGroup,
    limit: int = 100,
    before_message_id: int | None = None,
) -> list[dict[str, Any]]:
    project = _resolve_org_assistants_project(
        session,
        organization_id=group.organization_id,
    )
    context = session.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == chat_group_context_name(group.id),
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
        data.setdefault("reactions", [])
        data["group_id"] = group.id
        data["organization_id"] = group.organization_id
        messages.append(data)
    return messages


def toggle_group_message_reaction(
    session: Session,
    *,
    group: ChatGroup,
    message_id: int,
    user_id: str,
    emoji: str | None,
) -> dict[str, Any]:
    """Toggle one user's reaction on a group chat message."""
    project = _resolve_org_assistants_project(
        session,
        organization_id=group.organization_id,
    )
    context = session.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == chat_group_context_name(group.id),
        ),
    )
    if context is None:
        raise ValueError("Message not found")

    from sqlalchemy.orm.attributes import flag_modified

    query = (
        project_scoped_log_events(
            context.project_id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data["message_id"].astext == str(message_id),
        )
        .limit(1)
    )
    row = session.scalars(query).first()
    if row is None:
        raise ValueError("Message not found")

    data = dict(row.data)
    data["reactions"] = apply_user_reaction(
        data.get("reactions") if isinstance(data.get("reactions"), list) else [],
        user_id=user_id,
        emoji=emoji,
    )
    row.data = data
    flag_modified(row, "data")
    session.flush()

    data.setdefault("mentions", [])
    data.setdefault("attachments", [])
    data["group_id"] = group.id
    data["organization_id"] = group.organization_id
    return data


def search_group_messages(
    session: Session,
    *,
    group: ChatGroup,
    q: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    needle = q.strip()
    if not needle:
        return []
    project = _resolve_org_assistants_project(
        session,
        organization_id=group.organization_id,
    )
    context = session.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == chat_group_context_name(group.id),
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
        data["group_id"] = group.id
        data["organization_id"] = group.organization_id
        matches.append(data)
    return matches


def build_group_dispatch_payload(
    session: Session,
    *,
    group: ChatGroup,
    message: dict[str, Any],
    sender_email: str = "",
    exclude_assistant_id: int | None = None,
) -> dict[str, Any]:
    participants = chat_group_participants(session, group=group)
    return {
        "kind": "group",
        "organization_id": group.organization_id,
        "group_id": group.id,
        "message": message,
        "fanout_assistant_ids": [
            entry["assistant_id"]
            for entry in participants["assistants"]
            if entry["assistant_id"] != exclude_assistant_id
        ],
        "assistant_event": {
            "group_id": group.id,
            "group_name": group.name,
            "organization_id": group.organization_id,
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


def group_to_roster_dict(session: Session, group: ChatGroup) -> dict[str, Any]:
    participants = chat_group_participants(session, group=group)
    return {
        "group_id": group.id,
        "name": group.name,
        "created_by_user_id": group.created_by_user_id,
        "created_at": group.created_at,
        "member_user_ids": [h["user_id"] for h in participants["humans"]],
        "assistant_member_ids": [a["assistant_id"] for a in participants["assistants"]],
    }
