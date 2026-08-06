"""Ensure Contacts rows for every human and peer assistant on a call."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
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

# Platform identity columns on Contacts rows. These are the ``Contact`` model's
# own fields, which the assistant runtime already stamps on every system
# contact it seeds — attribution must key off them rather than a parallel
# vocabulary, or the two sides cannot see each other's rows.
CONTACT_USER_ID_FIELD = "user_id"
CONTACT_AGENT_ID_FIELD = "agent_id"
CONTACT_EMAIL_FIELD = "email_address"

# Response behaviour belongs to the assistant's owner. An adopted row keeps
# whatever they set, so joining a call never un-mutes a silenced contact.
OWNER_OWNED_CONTACT_FIELDS = ("should_respond", "response_policy")


def _next_contact_id(
    session: Session,
    *,
    context_id: int,
    project_id: int,
) -> int:
    """The next free ``contact_id`` in this context.

    Deliberately not owner-pruned: ``contact_id`` uniqueness is enforced
    context-scoped, so a max taken over one owner sub-partition can sit below
    rows filed under another and hand back an id that is already taken —
    every insert then collides on a value the pruned reads cannot even see.
    """
    max_id = session.scalar(
        project_scoped_log_events(
            project_id,
            func.max(cast(LogEvent.data.op("->>")("contact_id"), Numeric)),
        ).where(
            LogEventContext.context_id == context_id,
            LogEvent.data.has_key("contact_id"),
        ),
    )
    if max_id is None:
        return 2
    return int(max_id) + 1


def _find_contact_by_field(
    session: Session,
    *,
    context,
    key: str,
    value: str,
) -> LogEvent | None:
    """Find a Contacts row by one identity field.

    Owner-pruned first (partition prune), then without the owner
    sub-partition: Contacts uniqueness is enforced context-scoped, so a row
    can sit under a different ``owner_key`` than the context currently
    resolves to — invisible to the pruned read while still blocking an
    insert.
    """
    owner_key = single_owner_key(context.owner_scope, context.owner_id)
    owner_keys = [owner_key, None] if owner_key is not None else [None]
    for candidate in owner_keys:
        row = session.scalars(
            project_scoped_log_events(context.project_id, owner_key=candidate)
            .where(
                LogEventContext.context_id == context.id,
                LogEvent.data.op("->>")(key) == value,
            )
            .order_by(LogEvent.id.asc())
            .limit(1),
        ).first()
        if row is not None:
            return row
    return None


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


def _merged_contact_data(
    existing: dict[str, Any],
    entries: dict[str, Any],
    *,
    contact_id: int,
) -> dict[str, Any]:
    """Layer call attribution onto a row without demoting what it already holds."""
    merged = {**existing, **entries, "contact_id": contact_id}
    for field in OWNER_OWNED_CONTACT_FIELDS:
        if existing.get(field) is not None:
            merged[field] = existing[field]
    return merged


def _upsert_contact(
    session: Session,
    *,
    assistant: Assistant,
    entries: dict[str, Any],
    identities: list[tuple[str, str]],
) -> int:
    """Create or adopt this participant's Contacts row; return contact_id.

    ``identities`` is ordered most authoritative first: the platform id column
    (``user_id`` / ``agent_id``), then the identity fields a fresh insert would
    collide on. The fallback is what keeps a call startable — the runtime seeds
    org members into an assistant's Contacts keyed by ``email_address``, and
    those rows carry no platform id until something backfills it, so resolving
    on the id alone walks past the very row whose ``email_address`` the schema
    declares unique. Adoption stamps the platform id, so the cheap lookup wins
    every subsequent call.
    """
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

    for key, value in identities:
        existing = _find_contact_by_field(
            session,
            context=context,
            key=key,
            value=value,
        )
        if existing is None:
            continue
        contact_id = int(existing.data.get("contact_id"))
        merged = _merged_contact_data(existing.data, entries, contact_id=contact_id)
        if merged != existing.data:
            existing.data = merged
            flag_modified(existing, "data")
            session.flush()
        return contact_id

    contact_id = _next_contact_id(
        session,
        context_id=context.id,
        project_id=project.id,
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
            merged = _merged_contact_data(raced.data, entries, contact_id=contact_id)
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


def _assistant_display_name(assistant: Assistant) -> str:
    return (
        " ".join(
            part
            for part in [assistant.first_name or "", assistant.surname or ""]
            if part
        ).strip()
        or f"Assistant {assistant.agent_id}"
    )


def _assistant_email(session: Session, assistant: Assistant) -> str | None:
    """The assistant's provisioned email, as its Contacts rows carry it."""
    return session.scalar(
        select(AssistantContact.contact_value)
        .where(
            AssistantContact.assistant_id == assistant.agent_id,
            AssistantContact.contact_type == "email",
            AssistantContact.status == "active",
        )
        .order_by(AssistantContact.id.asc())
        .limit(1),
    )


def _without_nulls(entries: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields.

    A null carries no information, and writing one would overwrite a richer
    value on a row adopted from the runtime (which fills bios, timezones and
    phone numbers this side never sees).
    """
    return {key: value for key, value in entries.items() if value is not None}


def _human_entries(user: User) -> dict[str, Any]:
    return _without_nulls(
        {
            "first_name": user.name,
            "surname": user.last_name,
            "email_address": user.email,
            "job_title": user.job_title,
            "bio": user.bio,
            "timezone": user.timezone,
            "is_system": True,
            "should_respond": True,
            CONTACT_USER_ID_FIELD: user.id,
        },
    )


def _peer_assistant_entries(peer: Assistant) -> dict[str, Any]:
    return _without_nulls(
        {
            "first_name": peer.first_name or _assistant_display_name(peer),
            "surname": peer.surname,
            "is_system": True,
            "should_respond": False,
            CONTACT_AGENT_ID_FIELD: str(peer.agent_id),
        },
    )


def _ensure_owner_boss_contact(
    session: Session,
    *,
    assistant: Assistant,
    entries: dict[str, Any],
) -> int:
    """Map the assistant's own owner onto the reserved personal boss contact.

    The owner is already represented by ``PERSONAL_BOSS_CONTACT_ID``; minting a
    second row for them would duplicate them. Ensure the boss row exists, then
    stamp the platform id and latest human fields onto it so id-keyed lookups
    resolve here.
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
        merged = _merged_contact_data(boss.data, entries, contact_id=contact_id)
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
            identities = [(CONTACT_USER_ID_FIELD, user.id)]
            if user.email:
                identities.append((CONTACT_EMAIL_FIELD, user.email))
            contact_id = _upsert_contact(
                session,
                assistant=assistant,
                entries=_human_entries(user),
                identities=identities,
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
        identities = [(CONTACT_AGENT_ID_FIELD, str(peer.agent_id))]
        peer_email = _assistant_email(session, peer)
        if peer_email:
            identities.append((CONTACT_EMAIL_FIELD, peer_email))
        contact_id = _upsert_contact(
            session,
            assistant=assistant,
            entries=_peer_assistant_entries(peer),
            identities=identities,
        )
        roster.append(
            CallRosterMember(
                kind="assistant",
                user_id=None,
                assistant_id=peer.agent_id,
                display_name=_assistant_display_name(peer),
                contact_id=contact_id,
                email=peer_email,
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
