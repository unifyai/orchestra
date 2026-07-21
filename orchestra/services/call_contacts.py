"""Ensure Contacts rows for every human and peer assistant on a call."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Assistant,
    CallSession,
    LogEvent,
    LogEventContext,
    User,
)
from orchestra.db.scope import single_owner_key
from orchestra.services.assistant_bootstrap import (
    CONTACTS_AUTO_COUNTING,
    CONTACTS_CONTEXT_SUFFIX,
    CONTACTS_UNIQUE_KEYS,
    _assistant_context_name,
    _clear_stale_contact_unique_constraint,
    _create_log_entry,
    _find_contact_log_by_contact_id,
    _is_duplicate_contact_key_error,
    _lock_assistant_context,
    _resolve_assistants_project,
    ensure_context,
    ensure_owner_contact_row,
)
from orchestra.services.contact_membership_service import PERSONAL_BOSS_CONTACT_ID
from orchestra.web.api.calls.schema import CallRosterMember

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


def _find_contact_log_any_owner(
    session: Session,
    *,
    context,
    contact_id: int,
) -> LogEvent | None:
    """Find a Contacts row by ``contact_id`` **without** owner sub-partition
    pruning.

    The composite ``contact_id`` uniqueness is enforced context-scoped (not
    owner-scoped), so a row may exist under a different ``owner_key`` than the
    context currently resolves to and be invisible to the owner-pruned reads.
    This lookup sees it regardless, so a colliding insert can reuse it instead
    of failing.
    """
    return session.scalars(
        project_scoped_log_events(context.project_id)
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data.has_key("contact_id"),
            cast(LogEvent.data.op("->>")("contact_id"), Numeric) == contact_id,
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

        if not (
            isinstance(exc, HTTPException) and _is_duplicate_contact_key_error(exc)
        ):
            raise
        # A row with this contact_id already exists. Reuse it: first via the
        # owner-scoped read (correctly-scoped rows), then via a non-owner-pruned
        # lookup for rows stuck under a mismatched owner_key (the composite
        # uniqueness is context-scoped, so those block the insert while staying
        # invisible to owner-pruned reads).
        raced = _find_contact_log_by_contact_id(
            session,
            context=context,
            contact_id=contact_id,
        ) or _find_contact_log_any_owner(
            session,
            context=context,
            contact_id=contact_id,
        )
        if raced is not None:
            merged = {**raced.data, **entries}
            if merged != raced.data:
                raced.data = merged
                flag_modified(raced, "data")
                session.flush()
            return int(raced.data.get("contact_id"))
        # No live row backs the constraint: the lookup entry is stale. Drop it
        # and retry once (mirrors the owner-contact seeding self-heal).
        _clear_stale_contact_unique_constraint(
            session,
            context_id=context.id,
            contact_id=contact_id,
        )
        _create_log_entry(
            session,
            project=project,
            context=context,
            context_name=context_name,
            entries=entries,
        )
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


def _ensure_owner_boss_contact(
    session: Session,
    *,
    assistant: Assistant,
    entries: dict[str, Any],
) -> int:
    """Map the assistant's own owner onto the reserved personal boss contact.

    The owner is already represented by ``PERSONAL_BOSS_CONTACT_ID``; minting a
    parallel ``org_user_id``-keyed human would duplicate them (and, since the
    boss row carries no ``org_user_id``, the org-keyed lookup would re-mint on
    every call). Ensure the boss row exists, then stamp the org-call metadata
    and latest human fields onto it so future org-keyed lookups resolve here.
    """
    # Seed/refresh the boss row (returns its LogEvent id, not the contact_id).
    ensure_owner_contact_row(session, assistant=assistant)
    contact_id = PERSONAL_BOSS_CONTACT_ID
    project = _resolve_assistants_project(session, assistant=assistant)
    context_name = _assistant_context_name(assistant, CONTACTS_CONTEXT_SUFFIX)
    context = ensure_context(
        session,
        project_id=project.id,
        context_name=context_name,
        unique_keys=CONTACTS_UNIQUE_KEYS,
        auto_counting=CONTACTS_AUTO_COUNTING,
    )
    boss = _find_contact_log_by_contact_id(
        session,
        context=context,
        contact_id=contact_id,
    ) or _find_contact_log_any_owner(
        session,
        context=context,
        contact_id=contact_id,
    )
    if boss is not None:
        merged = {**boss.data, **entries, "contact_id": contact_id}
        if merged != boss.data:
            boss.data = merged
            flag_modified(boss, "data")
            session.flush()
    return contact_id


def _ensure_humans_for_assistant(
    session: Session,
    *,
    assistant: Assistant,
    call_session: CallSession,
) -> list[CallRosterMember]:
    roster: list[CallRosterMember] = []
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
        if user.id == assistant.user_id:
            contact_id = _ensure_owner_boss_contact(
                session,
                assistant=assistant,
                entries=_human_entries(user),
            )
        else:
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
            CallRosterMember(
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
) -> list[CallRosterMember]:
    roster: list[CallRosterMember] = []
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
            CallRosterMember(
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


def ensure_call_contacts(
    session: Session,
    *,
    call_session: CallSession,
    for_assistant_id: int,
) -> list[CallRosterMember]:
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
