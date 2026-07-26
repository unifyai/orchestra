"""Unified chat: thread access rules, payload shaping, and hosted dispatch.

Every Console chat surface (human DM, assistant DM, team, group) persists to
the Postgres-backed unified store (``chat_thread`` / ``chat_message``) and is
delivered by the hosted communication layer (adapters ``POST /unify/chat``):
one Console frame on the relevant Pub/Sub topic, plus one standard
``unify_message`` envelope per listed assistant runtime. Assistant
Transcripts never back a Console surface — runtimes only mirror conversations
into their own Transcripts as memory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.dao.chat_dao import (
    KIND_ASSISTANT_DM,
    KIND_ASSISTANT_PEER_DM,
    KIND_DM,
    KIND_GROUP,
    KIND_TEAM,
)
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    ChatMessage,
    ChatThread,
    Team,
    User,
)

SENDER_KIND_USER = "user"
SENDER_KIND_ASSISTANT = "assistant"


def user_display_name(user: User) -> str:
    return " ".join(part for part in [user.name, user.last_name] if part) or user.email


def assistant_display_name(assistant: Assistant) -> str:
    return (
        " ".join(part for part in [assistant.first_name, assistant.surname] if part)
        or f"Assistant {assistant.agent_id}"
    )


def _normalize_reaction_emoji(emoji: str | None) -> str | None:
    if emoji is None:
        return None
    cleaned = emoji.strip()
    return cleaned or None


def apply_user_reaction(
    existing: list[dict[str, Any]] | None,
    *,
    user_id: str,
    emoji: str | None,
) -> list[dict[str, Any]]:
    """Add, change, or remove one user's emoji reaction on a message."""
    reactions = [
        dict(item)
        for item in (existing or [])
        if isinstance(item, dict) and item.get("user_id")
    ]
    index = next(
        (i for i, item in enumerate(reactions) if str(item.get("user_id")) == user_id),
        -1,
    )
    normalized = _normalize_reaction_emoji(emoji)
    if normalized is None:
        if index == -1:
            return reactions
        reactions.pop(index)
        return reactions

    next_reaction = {
        "user_id": user_id,
        "emoji": normalized,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if index == -1:
        reactions.append(next_reaction)
        return reactions
    if reactions[index].get("emoji") == normalized:
        reactions.pop(index)
        return reactions
    reactions[index] = next_reaction
    return reactions


def thread_summary(thread: ChatThread) -> dict[str, Any]:
    """Wire shape for one thread (REST responses and Console frames)."""
    assistant_ids: list[int] = []
    if thread.kind == KIND_ASSISTANT_PEER_DM:
        if thread.assistant_id is not None and thread.peer_assistant_id is not None:
            assistant_ids = [thread.assistant_id, thread.peer_assistant_id]
    return {
        "thread_id": thread.id,
        "kind": thread.kind,
        "organization_id": thread.organization_id,
        "user_ids": (
            [thread.user_a_id, thread.user_b_id] if thread.kind == KIND_DM else []
        ),
        "assistant_ids": assistant_ids,
        "assistant_id": thread.assistant_id,
        "peer_assistant_id": thread.peer_assistant_id,
        "user_id": thread.user_id,
        "team_id": thread.team_id,
        "group_id": thread.group_id,
    }


def message_payload(message: ChatMessage, thread: ChatThread) -> dict[str, Any]:
    """Wire shape for one message (REST responses and Console frames).

    Attachment signed-URL enrichment is layered on by the API views; this
    payload carries the stored attachment dicts as-is.
    """
    sender_kind = (
        SENDER_KIND_ASSISTANT
        if message.sender_assistant_id is not None
        else SENDER_KIND_USER
    )
    payload = thread_summary(thread)
    payload.update(
        {
            "id": message.id,
            "sender_kind": sender_kind,
            "sender_user_id": message.sender_user_id,
            "sender_assistant_id": message.sender_assistant_id,
            "sender_name": message.sender_name,
            "content": message.content,
            "mentions": message.mentions or [],
            "attachments": message.attachments or [],
            "reactions": message.reactions or [],
            "call_id": message.call_id,
            "timestamp": (
                message.created_at.isoformat() if message.created_at else None
            ),
        },
    )
    return payload


def human_can_access_thread(
    session: Session,
    *,
    thread: ChatThread,
    user_id: str,
) -> bool:
    """Whether one human may read/post in a thread (membership per kind)."""
    if thread.kind == KIND_DM:
        return user_id in (thread.user_a_id, thread.user_b_id)
    if thread.kind == KIND_ASSISTANT_DM:
        return thread.user_id == user_id
    if thread.kind == KIND_TEAM:
        return TeamDAO(session).is_team_member(thread.team_id, user_id)
    if thread.kind == KIND_GROUP:
        from orchestra.services.chat_group_service import is_human_group_member

        return is_human_group_member(
            session,
            group_id=thread.group_id,
            user_id=user_id,
        )
    return False


def assistant_can_access_thread(
    session: Session,
    *,
    thread: ChatThread,
    assistant_id: int,
) -> bool:
    """Whether one assistant may post in a thread (membership per kind)."""
    if thread.kind == KIND_ASSISTANT_DM:
        return thread.assistant_id == assistant_id
    if thread.kind == KIND_ASSISTANT_PEER_DM:
        return assistant_id in (thread.assistant_id, thread.peer_assistant_id)
    if thread.kind == KIND_TEAM:
        return (
            TeamDAO(session).get_assistant_membership(
                team_id=thread.team_id,
                assistant_id=assistant_id,
            )
            is not None
        )
    if thread.kind == KIND_GROUP:
        from orchestra.services.chat_group_service import is_assistant_group_member

        return is_assistant_group_member(
            session,
            group_id=thread.group_id,
            assistant_id=assistant_id,
        )
    return False


def _fanout_assistant_ids(
    session: Session,
    *,
    thread: ChatThread,
    exclude_assistant_id: int | None,
) -> list[int]:
    """Assistant runtimes that receive a copy of one thread message.

    Team/group messages fan out to every non-coordinator member assistant
    (minus the author); assistant DMs fan out to the single assistant when a
    human sent the message; assistant peer DMs fan out to the other peer.
    Human DMs never involve an assistant.
    """
    if thread.kind == KIND_ASSISTANT_DM:
        ids = [thread.assistant_id]
    elif thread.kind == KIND_ASSISTANT_PEER_DM:
        ids = [thread.assistant_id, thread.peer_assistant_id]
    elif thread.kind == KIND_TEAM:
        from orchestra.services.org_chat_service import team_chat_participants

        team = session.get(Team, thread.team_id)
        participants = team_chat_participants(session, team=team)
        ids = [entry["assistant_id"] for entry in participants["assistants"]]
    elif thread.kind == KIND_GROUP:
        from orchestra.services.chat_group_service import (
            chat_group_participants,
            get_active_group,
        )

        group = get_active_group(
            session,
            organization_id=thread.organization_id,
            group_id=thread.group_id,
        )
        if group is None:
            return []
        participants = chat_group_participants(session, group=group)
        ids = [entry["assistant_id"] for entry in participants["assistants"]]
    else:
        return []
    return [aid for aid in ids if aid is not None and aid != exclude_assistant_id]


def build_chat_dispatch_payload(
    session: Session,
    *,
    thread: ChatThread,
    message: dict[str, Any],
    sender_email: str = "",
    exclude_assistant_id: int | None = None,
) -> dict[str, Any]:
    """Build the adapters ``POST /unify/chat`` payload for one message.

    ``message`` is the enriched :func:`message_payload` (the Console frame).
    ``exclude_assistant_id`` skips the authoring assistant — it already knows
    what it said. ``sender_email`` lets each receiving runtime resolve the
    sender against its own Contacts table when the sender is not that
    assistant's owner.
    """
    thread_name = ""
    if thread.kind == KIND_TEAM:
        team = session.get(Team, thread.team_id)
        thread_name = team.name if team else ""
    elif thread.kind == KIND_GROUP:
        from orchestra.services.chat_group_service import get_active_group

        group = get_active_group(
            session,
            organization_id=thread.organization_id,
            group_id=thread.group_id,
        )
        thread_name = group.name if group else ""

    return {
        "kind": thread.kind,
        "thread_id": thread.id,
        "organization_id": thread.organization_id,
        "assistant_id": thread.assistant_id,
        "peer_assistant_id": thread.peer_assistant_id,
        "team_id": thread.team_id,
        "group_id": thread.group_id,
        "message": message,
        "fanout_assistant_ids": _fanout_assistant_ids(
            session,
            thread=thread,
            exclude_assistant_id=exclude_assistant_id,
        ),
        "assistant_event": {
            "thread_id": thread.id,
            "thread_kind": thread.kind,
            "chat_message_id": message.get("id"),
            "team_id": thread.team_id,
            "team_name": thread_name if thread.kind == KIND_TEAM else "",
            "group_id": thread.group_id,
            "group_name": thread_name if thread.kind == KIND_GROUP else "",
            "organization_id": thread.organization_id,
            "body": message.get("content") or "",
            "sender_kind": message.get("sender_kind") or SENDER_KIND_USER,
            "sender_user_id": message.get("sender_user_id") or "",
            "sender_assistant_id": message.get("sender_assistant_id"),
            "sender_email": sender_email,
            "sender_name": message.get("sender_name") or "",
            "attachments": message.get("attachments") or [],
        },
    }
