"""MS Teams bot inbound routing.

The Bot-Framework analogue of :mod:`orchestra.services.slack_dispatcher`.
Given a normalized Teams activity (already JWT-verified by the adapter,
with the bot ``<at>`` mention already stripped into ``addressed_text``),
decide which assistant in the tenant's Unify owner scope should receive
it.

Routing tree — deliberately parallel to Slack, with one structural
difference driven by how Teams delivers activities:

``groupChat`` / ``channel`` + bot mention + token → explicit addressing,
    resolves via :meth:`AssistantDAO.resolve_token`. Ambiguous / unknown
    tokens fall back to the coordinator with a ``routing_metadata`` hint.

``personal`` (1:1 chat) → established conversation route wins; otherwise
    the coordinator handles the chat. Token addressing is intentionally
    *not* honored in 1:1 chats (there is no natural ``@bot Name`` gesture
    there).

Unlike Slack — where the bot subscribes to *all* channel messages and so
must drop untokened/unbound channel traffic — Teams only delivers a
channel/group activity to the bot when it is **@mentioned**. Every
activity we receive is therefore intended for the bot, so the terminal
fallback is the **coordinator**, never a silent drop. The only ``None``
(drop) outcomes are: no install, a *pending* (unbound) install, the bot's
own echo, or an owner with no Coordinator at all.

Side effects are limited to upserting / touching conversation routes.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.ms_teams_bot_dao import MsTeamsBotDAO
from orchestra.db.models.orchestra_models import Assistant, MsTeamsBotInstall, User

logger = logging.getLogger(__name__)

# Punctuation commonly attached to an addressing token when a human types
# naturally, e.g. ``Lily, …`` or ``Lily: …``. Stripped from each word's
# edges before token resolution. Hyphen/apostrophe are excluded so names
# like ``Mary-Jane`` and ``O'Brien`` survive.
_TOKEN_EDGE_PUNCT = ',.;:!?"`()[]{}<>'


def _clean_word(word: str) -> str:
    """Strip surrounding punctuation from a single addressing word."""
    return word.strip(_TOKEN_EDGE_PUNCT)


@dataclass
class SenderIdentity:
    """Best-effort identity of the human who sent an inbound Teams activity.

    Teams activities carry ``from.name`` (AAD display name) and
    ``from.aadObjectId`` on the *first* pass; the sender's email is not in
    the activity and requires a roster / Graph lookup, which the adapter
    performs on a *second* dispatch pass for org installs. On the first
    pass ``provided`` is ``False``: the dispatcher routes provisionally to
    the org's deterministic Coordinator and asks the caller to re-dispatch
    with identity for per-member precision.
    """

    provided: bool = False
    email: Optional[str] = None
    display_name: Optional[str] = None
    aad_object_id: Optional[str] = None


@dataclass
class MsTeamsBotInboundResolution:
    """Outcome of routing a single inbound Teams activity."""

    install: MsTeamsBotInstall
    assistant_id: int
    """Final recipient. Always a real assistant row in the install's scope."""

    conversation_id_for_route: str
    """Key used to upsert a conversation route if persisting."""

    conversation_reference: Optional[str] = None
    """Serialized Bot Framework ConversationReference for proactive reply."""

    route_persisted: bool = False
    """True when a conversation route was upserted (or touched)."""

    routing_metadata: dict[str, Any] = field(default_factory=dict)
    """Structured hints surfaced to the receiving assistant.

    Examples:
        ``{"reason": "token_addressed", "token": "lily"}``
        ``{"reason": "unknown_token", "token": "alx"}``
        ``{"reason": "ambiguous_token", "token": "alex", "candidates": [...]}``
        ``{"reason": "initial_chat"}``
        ``{"reason": "channel_binding"}``
        ``{"reason": "mentioned_fallback"}``
    """

    needs_sender_identity: bool = False
    """True when this is a *provisional* coordinator route for an org
    install and the caller should re-dispatch with the sender's identity
    (email / name) so the activity can be pinned to the sender's own
    workspace Coordinator. The provisional ``assistant_id`` is still a
    valid recipient, so ignoring this flag degrades to deterministic
    org-Coordinator routing rather than dropping the activity.
    """


def _resolve_addressing(
    session: Session,
    install: MsTeamsBotInstall,
    rest: str,
) -> tuple[list[Assistant], str]:
    """Resolve the words addressed to the bot to candidate assistants.

    Escalates from most- to least-specific so first-name collisions are
    still routable: an unambiguous ``"first surname"`` hit wins, otherwise
    the single leading word is resolved against the numeric agent id or
    first name via :meth:`AssistantDAO.resolve_token`.

    Returns the candidate list together with the token string that
    produced it (surfaced in ``routing_metadata``).
    """
    dao = AssistantDAO(session)
    scope = _scope_kwargs(install)
    words = [cleaned for cleaned in (_clean_word(w) for w in rest.split()) if cleaned]
    if not words:
        return [], ""
    if len(words) >= 2:
        full = f"{words[0]} {words[1]}"
        candidates = dao.resolve_token(full, **scope)
        if len(candidates) == 1:
            return candidates, full
    leading = words[0]
    return dao.resolve_token(leading, **scope), leading


def _assistant_label(assistant: Assistant) -> dict[str, Any]:
    """Compact summary of an assistant for ambiguous-token hints."""
    return {
        "assistant_id": assistant.agent_id,
        "first_name": assistant.first_name,
        "surname": assistant.surname,
    }


def _scope_kwargs(install: MsTeamsBotInstall) -> dict[str, Any]:
    """Translate a *bound* install's owner into AssistantDAO kwargs.

    A bound ``MsTeamsBotInstall`` has exactly one of ``organization_id`` /
    ``user_id`` set; pending installs are filtered out before this is
    reached. We forward whichever field is populated.
    """
    if install.organization_id is not None:
        return {"organization_id": install.organization_id}
    return {"user_id": install.user_id}


def resolve_inbound(
    session: Session,
    *,
    tenant_id: str,
    conversation_id: str,
    conversation_type: str,
    channel_id: Optional[str],
    sender_aad_object_id: str,
    bot_mentioned: bool,
    addressed_text: str,
    conversation_reference: Optional[str] = None,
    sender_email: Optional[str] = None,
    sender_display_name: Optional[str] = None,
    sender_identity_provided: bool = False,
) -> Optional[MsTeamsBotInboundResolution]:
    """Route a single inbound Teams activity.

    :param conversation_type: ``"personal"`` for 1:1 chats, ``"groupChat"``
        for group chats, ``"channel"`` for team-channel messages.
    :param channel_id: Teams channel identity
        (``channelData.channel.id``) for ``channel`` activities, else
        ``None``. Used for channel bindings.
    :param sender_aad_object_id: AAD object id of the human sender
        (``from.aadObjectId``). Used to suppress the bot's own echoes and
        for identity matching.
    :param bot_mentioned: ``True`` if the bot was @mentioned in this
        activity. Channel / group activities are only delivered to the bot
        when mentioned; 1:1 activities are always delivered.
    :param addressed_text: The message text with the bot ``<at>`` mention
        already stripped by the adapter. Non-empty + ``bot_mentioned`` in a
        group/channel is treated as explicit token addressing.
    :param conversation_reference: Serialized Bot Framework
        ConversationReference captured on inbound, stored on the route for
        proactive outbound replies.
    :param sender_email: Sender email, if the adapter resolved it via a
        roster / Graph lookup (org installs, second dispatch pass).
    :param sender_display_name: Sender AAD display name (``from.name``).
    :param sender_identity_provided: ``True`` once the adapter has attempted
        the identity lookup, distinguishing the first dispatch pass from the
        second so coordinator routing does not loop.

    Returns ``None`` when no assistant should receive the activity (no
    install, a *pending* / unbound install, the bot's own echo, or an owner
    with no Coordinator).
    """
    dao = MsTeamsBotDAO(session)
    install = dao.get_install_by_tenant(tenant_id)
    if install is None:
        return None

    # A pending install (Teams Store install not yet bound to a Unify
    # owner) has nothing to route to yet.
    if install.organization_id is None and install.user_id is None:
        logger.info(
            "Dropping Teams activity for pending (unbound) install %s " "(tenant %s).",
            install.id,
            tenant_id,
        )
        return None

    identity = SenderIdentity(
        provided=sender_identity_provided,
        email=sender_email,
        display_name=sender_display_name,
        aad_object_id=sender_aad_object_id,
    )

    is_personal = conversation_type == "personal"
    # A non-empty addressed remainder only counts as token addressing when
    # the bot was actually mentioned and we are not in a 1:1 chat.
    rest = (
        addressed_text.strip()
        if (bot_mentioned and not is_personal and addressed_text.strip())
        else None
    )

    if is_personal:
        return _resolve_personal(
            session=session,
            dao=dao,
            install=install,
            conversation_id=conversation_id,
            conversation_reference=conversation_reference,
            identity=identity,
        )

    return _resolve_group_or_channel(
        session=session,
        dao=dao,
        install=install,
        conversation_id=conversation_id,
        channel_id=channel_id,
        rest=rest,
        conversation_reference=conversation_reference,
        identity=identity,
    )


def _normalize_name(name: str) -> str:
    """Lower-case and strip accents/punctuation for tolerant name compares."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", stripped).lower()
    return re.sub(r"\s+", " ", cleaned).strip()


def _coordinator_owners(session: Session, organization_id: int) -> list[User]:
    """Users who own a Coordinator assistant in this organization."""
    return (
        session.query(User)
        .join(Assistant, Assistant.user_id == User.id)
        .filter(
            Assistant.organization_id == organization_id,
            Assistant.is_coordinator.is_(True),
        )
        .all()
    )


def _resolve_member_user_id(
    session: Session,
    organization_id: int,
    identity: SenderIdentity,
) -> Optional[str]:
    """Map a Teams sender to the org member who owns their Coordinator.

    An exact email match wins; otherwise a unique full-name match (either
    ordering) on the member's profile name. Ambiguous name matches are
    refused so we never route to the wrong person.
    """
    owners = _coordinator_owners(session, organization_id)
    if not owners:
        return None

    email = (identity.email or "").strip().lower()
    if email:
        for owner in owners:
            if (owner.email or "").strip().lower() == email:
                return owner.id

    target = _normalize_name(identity.display_name or "")
    if target:
        matches = [
            owner.id
            for owner in owners
            if target
            in (
                _normalize_name(f"{owner.name or ''} {owner.last_name or ''}"),
                _normalize_name(f"{owner.last_name or ''} {owner.name or ''}"),
            )
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _resolve_coordinator(
    session: Session,
    install: MsTeamsBotInstall,
    identity: SenderIdentity,
) -> tuple[Optional[Assistant], bool]:
    """Resolve the Coordinator for untokened / ambiguous traffic.

    Returns ``(coordinator, needs_sender_identity)``:

    * **Personal install** — the user's single personal Coordinator,
      sender-independent. Never needs identity.
    * **Org install, identity provided** — the sender's *own* workspace
      Coordinator when the sender maps to a member who owns one; otherwise
      the org's deterministic Coordinator as a safe fallback.
    * **Org install, identity not yet provided** — the org's deterministic
      Coordinator with ``needs_sender_identity=True`` so the caller can
      re-dispatch with the sender's identity for per-member precision.

    A ``None`` coordinator means the owner has none at all; the caller
    drops the activity rather than crashing.
    """
    dao = AssistantDAO(session)
    if install.organization_id is None:
        return dao.coordinator(user_id=install.user_id), False

    organization_id = install.organization_id
    if identity.provided:
        member_user_id = _resolve_member_user_id(
            session,
            organization_id,
            identity,
        )
        if member_user_id is not None:
            member_coordinator = dao.coordinator(
                user_id=member_user_id,
                organization_id=organization_id,
            )
            if member_coordinator is not None:
                return member_coordinator, False
        return dao.coordinator(organization_id=organization_id), False

    return dao.coordinator(organization_id=organization_id), True


def _resolve_personal(
    *,
    session: Session,
    dao: MsTeamsBotDAO,
    install: MsTeamsBotInstall,
    conversation_id: str,
    conversation_reference: Optional[str],
    identity: SenderIdentity,
) -> Optional[MsTeamsBotInboundResolution]:
    existing = dao.get_conversation_route(install.id, conversation_id)
    if existing is not None:
        dao.touch_conversation_route(existing)
        if conversation_reference is not None:
            existing.conversation_reference = conversation_reference
            session.flush()
        return MsTeamsBotInboundResolution(
            install=install,
            assistant_id=existing.assistant_id,
            conversation_id_for_route=conversation_id,
            conversation_reference=existing.conversation_reference,
            route_persisted=True,
        )

    coordinator, needs_identity = _resolve_coordinator(session, install, identity)
    if coordinator is None:
        return None
    if not needs_identity:
        dao.upsert_conversation_route(
            install.id,
            conversation_id,
            coordinator.agent_id,
            conversation_reference=conversation_reference,
        )
    return MsTeamsBotInboundResolution(
        install=install,
        assistant_id=coordinator.agent_id,
        conversation_id_for_route=conversation_id,
        conversation_reference=conversation_reference,
        route_persisted=not needs_identity,
        routing_metadata={"reason": "initial_chat"},
        needs_sender_identity=needs_identity,
    )


def _resolve_group_or_channel(
    *,
    session: Session,
    dao: MsTeamsBotDAO,
    install: MsTeamsBotInstall,
    conversation_id: str,
    channel_id: Optional[str],
    rest: Optional[str],
    conversation_reference: Optional[str],
    identity: SenderIdentity,
) -> Optional[MsTeamsBotInboundResolution]:
    if rest is not None:
        candidates, token = _resolve_addressing(session, install, rest)
        if len(candidates) == 1:
            assistant = candidates[0]
            dao.upsert_conversation_route(
                install.id,
                conversation_id,
                assistant.agent_id,
                conversation_reference=conversation_reference,
            )
            return MsTeamsBotInboundResolution(
                install=install,
                assistant_id=assistant.agent_id,
                conversation_id_for_route=conversation_id,
                conversation_reference=conversation_reference,
                route_persisted=True,
                routing_metadata={"reason": "token_addressed", "token": token},
            )

        coordinator, needs_identity = _resolve_coordinator(
            session,
            install,
            identity,
        )
        if coordinator is None:
            return None
        # The coordinator takes the conversation so it can clarify in
        # place. A provisional (needs-identity) route is still persisted; a
        # second dispatch with the sender's identity re-pins it if that
        # differs.
        dao.upsert_conversation_route(
            install.id,
            conversation_id,
            coordinator.agent_id,
            conversation_reference=conversation_reference,
        )
        meta: dict[str, Any] = {
            "reason": "unknown_token" if not candidates else "ambiguous_token",
            "token": token,
        }
        if candidates:
            meta["candidates"] = [_assistant_label(a) for a in candidates]
        return MsTeamsBotInboundResolution(
            install=install,
            assistant_id=coordinator.agent_id,
            conversation_id_for_route=conversation_id,
            conversation_reference=conversation_reference,
            route_persisted=True,
            routing_metadata=meta,
            needs_sender_identity=needs_identity,
        )

    # No explicit token: an established conversation route wins.
    existing = dao.get_conversation_route(install.id, conversation_id)
    if existing is not None:
        dao.touch_conversation_route(existing)
        if conversation_reference is not None:
            existing.conversation_reference = conversation_reference
            session.flush()
        return MsTeamsBotInboundResolution(
            install=install,
            assistant_id=existing.assistant_id,
            conversation_id_for_route=conversation_id,
            conversation_reference=existing.conversation_reference,
            route_persisted=True,
        )

    # Otherwise a channel binding sets the default recipient.
    if channel_id is not None:
        binding = dao.get_channel_binding(install.id, channel_id)
        if binding is not None:
            dao.upsert_conversation_route(
                install.id,
                conversation_id,
                binding.assistant_id,
                conversation_reference=conversation_reference,
            )
            return MsTeamsBotInboundResolution(
                install=install,
                assistant_id=binding.assistant_id,
                conversation_id_for_route=conversation_id,
                conversation_reference=conversation_reference,
                route_persisted=True,
                routing_metadata={"reason": "channel_binding"},
            )

    # The bot was mentioned but nothing else addresses it — Teams only
    # delivers mentioned activities, so this is genuine intent to engage.
    # Fall back to the coordinator rather than dropping.
    coordinator, needs_identity = _resolve_coordinator(session, install, identity)
    if coordinator is None:
        return None
    if not needs_identity:
        dao.upsert_conversation_route(
            install.id,
            conversation_id,
            coordinator.agent_id,
            conversation_reference=conversation_reference,
        )
    return MsTeamsBotInboundResolution(
        install=install,
        assistant_id=coordinator.agent_id,
        conversation_id_for_route=conversation_id,
        conversation_reference=conversation_reference,
        route_persisted=not needs_identity,
        routing_metadata={"reason": "mentioned_fallback"},
        needs_sender_identity=needs_identity,
    )
