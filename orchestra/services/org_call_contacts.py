"""Ensure Contacts rows for every human and peer assistant on an org call."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Assistant,
    LogEvent,
    LogEventContext,
    OrgCallSession,
    User,
)
from orchestra.db.scope import single_owner_key
from orchestra.services.assistant_bootstrap import (
    CONTACTS_AUTO_COUNTING,
    CONTACTS_CONTEXT_SUFFIX,
    CONTACTS_UNIQUE_KEYS,
    _assistant_context_name,
    _create_log_entry,
    _find_contact_log_by_contact_id,
    _is_duplicate_contact_key_error,
    _lock_assistant_context,
    _resolve_assistants_project,
    ensure_context,
)
from orchestra.web.api.org_chat.schema import OrgCallRosterMember

# Metadata keys stored on Contacts rows for stable org-call attribution.
ORG_CALL_USER_ID_KEY = "org_user_id"
ORG_CALL_PEER_ASSISTANT_ID_KEY = "peer_assistant_id"


def _next_contact_id(
    session: Session,
    *,
    context_id: int,
    project_id: int,
    owner_key: str | None = None,
) -> int:
    max_id = session.scalar(
        project_scoped_log_events(
            project_id,
            func.max(cast(LogEvent.data.op("->>")("contact_id"), Numeric)),
            owner_key=owner_key,
        ).where(
            LogEventContext.context_id == context_id,
            LogEvent.data.has_key("contact_id"),
        ),
    )
    if max_id is None:
        return 2
    return int(max_id) + 1


def _find_contact_by_meta(
    session: Session,
    *,
    context,
    key: str,
    value: str,
) -> LogEvent | None:
    return session.scalars(
        project_scoped_log_events(
            context.project_id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data.op("->>")(key) == value,
        )
        .order_by(LogEvent.id.asc())
        .limit(1),
    ).first()


def _upsert_contact(
    session: Session,
    *,
    assistant: Assistant,
    entries: dict[str, Any],
    lookup_key: str,
    lookup_value: str,
) -> int:
    """Create or refresh a Contacts row; return contact_id."""
    _lock_assistant_context(
        session,
        assistant=assistant,
        suffix=CONTACTS_CONTEXT_SUFFIX,
    )
    project = _resolve_assistants_project(session, assistant=assistant)
    context_name = _assistant_context_name(assistant, CONTACTS_CONTEXT_SUFFIX)
    context = ensure_context(
        session,
        project_id=project.id,
        context_name=context_name,
        unique_keys=CONTACTS_UNIQUE_KEYS,
        auto_counting=CONTACTS_AUTO_COUNTING,
    )

    existing = _find_contact_by_meta(
        session,
        context=context,
        key=lookup_key,
        value=lookup_value,
    )
    if existing is not None:
        contact_id = int(existing.data.get("contact_id"))
        merged = {**existing.data, **entries, "contact_id": contact_id}
        if merged != existing.data:
            existing.data = merged
            flag_modified(existing, "data")
            session.flush()
        return contact_id

    contact_id = _next_contact_id(
        session,
        context_id=context.id,
        project_id=project.id,
        owner_key=single_owner_key(context.owner_scope, context.owner_id),
    )
    entries = {**entries, "contact_id": contact_id}
    try:
        _create_log_entry(
            session,
            project=project,
            context=context,
            context_name=context_name,
            entries=entries,
        )
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and _is_duplicate_contact_key_error(exc):
            raced = _find_contact_log_by_contact_id(
                session,
                context=context,
                contact_id=contact_id,
            )
            if raced is not None:
                return int(raced.data.get("contact_id"))
        raise
    session.flush()
    return contact_id


def _human_entries(user: User) -> dict[str, Any]:
    return {
        "first_name": user.name or "",
        "surname": user.last_name or "",
        "email_address": user.email,
        "job_title": user.job_title,
        "bio": user.bio,
        "timezone": user.timezone,
        "is_system": True,
        "should_respond": True,
        ORG_CALL_USER_ID_KEY: user.id,
    }


def _peer_assistant_entries(peer: Assistant) -> dict[str, Any]:
    display = (
        " ".join(
            part for part in [peer.first_name or "", peer.surname or ""] if part
        ).strip()
        or f"Assistant {peer.agent_id}"
    )
    return {
        "first_name": peer.first_name or display,
        "surname": peer.surname or "",
        "email_address": None,
        "is_system": True,
        "should_respond": False,
        ORG_CALL_PEER_ASSISTANT_ID_KEY: str(peer.agent_id),
    }


def _ensure_humans_for_assistant(
    session: Session,
    *,
    assistant: Assistant,
    call_session: OrgCallSession,
) -> list[OrgCallRosterMember]:
    roster: list[OrgCallRosterMember] = []
    user_ids = [p.user_id for p in (call_session.participants or [])]
    if not user_ids:
        return roster
    users = {
        u.id: u
        for u in session.scalars(select(User).where(User.id.in_(user_ids))).all()
    }
    for user_id in user_ids:
        user = users.get(user_id)
        if user is None:
            continue
        contact_id = _upsert_contact(
            session,
            assistant=assistant,
            entries=_human_entries(user),
            lookup_key=ORG_CALL_USER_ID_KEY,
            lookup_value=user.id,
        )
        display = " ".join(
            part for part in [user.name or "", user.last_name or ""] if part
        ).strip() or (user.email or user.id)
        roster.append(
            OrgCallRosterMember(
                kind="human",
                user_id=user.id,
                assistant_id=None,
                display_name=display,
                contact_id=contact_id,
                email=user.email,
            ),
        )
    return roster


def _ensure_peer_assistants_for_assistant(
    session: Session,
    *,
    assistant: Assistant,
    peer_ids: list[int],
) -> list[OrgCallRosterMember]:
    roster: list[OrgCallRosterMember] = []
    peers = [
        p
        for p in session.scalars(
            select(Assistant).where(Assistant.agent_id.in_(peer_ids)),
        ).all()
        if p.agent_id != assistant.agent_id
    ]
    for peer in peers:
        contact_id = _upsert_contact(
            session,
            assistant=assistant,
            entries=_peer_assistant_entries(peer),
            lookup_key=ORG_CALL_PEER_ASSISTANT_ID_KEY,
            lookup_value=str(peer.agent_id),
        )
        roster.append(
            OrgCallRosterMember(
                kind="assistant",
                user_id=None,
                assistant_id=peer.agent_id,
                display_name=(
                    " ".join(
                        part
                        for part in [peer.first_name or "", peer.surname or ""]
                        if part
                    ).strip()
                    or f"Assistant {peer.agent_id}"
                ),
                contact_id=contact_id,
                email=None,
            ),
        )
    return roster


def ensure_org_call_contacts(
    session: Session,
    *,
    call_session: OrgCallSession,
    for_assistant_id: int,
) -> list[OrgCallRosterMember]:
    """Ensure Contacts for humans + peer assistants; return roster for ``for_assistant_id``.

    Also backfills peer Contacts on every other assistant already on the call so
    mid-call adds remain attributable on both sides.
    """
    assistant = session.get(Assistant, for_assistant_id)
    if assistant is None:
        raise ValueError(f"Assistant {for_assistant_id} not found")

    assistant_ids = [int(a) for a in (call_session.assistant_ids or [])]
    if for_assistant_id not in assistant_ids:
        assistant_ids.append(for_assistant_id)

    # Ensure for the joining assistant first — this is the roster we return.
    human_roster = _ensure_humans_for_assistant(
        session,
        assistant=assistant,
        call_session=call_session,
    )
    peer_roster = _ensure_peer_assistants_for_assistant(
        session,
        assistant=assistant,
        peer_ids=assistant_ids,
    )

    # Backfill peer links on every other assistant already on the call.
    for other_id in assistant_ids:
        if other_id == for_assistant_id:
            continue
        other = session.get(Assistant, other_id)
        if other is None:
            continue
        _ensure_humans_for_assistant(
            session,
            assistant=other,
            call_session=call_session,
        )
        _ensure_peer_assistants_for_assistant(
            session,
            assistant=other,
            peer_ids=assistant_ids,
        )

    session.flush()
    return [*human_roster, *peer_roster]
