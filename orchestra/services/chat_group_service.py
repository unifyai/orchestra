"""Chat groups: lightweight multi-party org chats (humans + assistants).

Group *messages* live in the unified chat store
(:mod:`orchestra.db.dao.chat_dao`) and are served by the ``/chat`` API. This
module owns group membership CRUD and participant resolution. Realtime
delivery mirrors team chat: Console SSE via adapters ``POST /unify/chat``
plus ``unify_message`` fan-out to every roster-visible member assistant
(private single-player coordinators are excluded).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orchestra.db.models.orchestra_models import (
    Assistant,
    ChatGroup,
    ChatGroupMember,
    Project,
    User,
)
from orchestra.db.scope import OwnerScope, purge_owner
from orchestra.services.org_chat_service import ASSISTANTS_PROJECT_NAME

logger = logging.getLogger(__name__)


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
    """Mark deleted and purge legacy Groups/{id}/... contexts.

    The unified-store thread and messages are retained (the group row is
    soft-deleted, so the thread is unreachable); the context purge only
    clears legacy log-backed GroupChat data.
    """
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
        if not assistant.is_private_coordinator
    ]
    return {"humans": humans, "assistants": assistants}


def group_to_roster_dict(session: Session, group: ChatGroup) -> dict[str, Any]:
    participants = chat_group_participants(session, group=group)
    return {
        "group_id": group.id,
        "name": group.name,
        "icon": group.icon,
        "created_by_user_id": group.created_by_user_id,
        "created_at": group.created_at,
        "member_user_ids": [h["user_id"] for h in participants["humans"]],
        "assistant_member_ids": [a["assistant_id"] for a in participants["assistants"]],
    }
