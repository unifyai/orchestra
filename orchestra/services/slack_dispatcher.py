"""Slack inbound routing.

Given a normalized Slack event (already verified by the gateway), decide
which assistant in the workspace's organization should receive it. This is
the single source of truth for the routing tree the v7 design encodes:

``app_mention`` + token  → explicit addressing, resolves via
    :meth:`AssistantDAO.resolve_token`. Ambiguous or unknown tokens fall
    back to the coordinator with a structured ``routing_metadata`` hint so
    the assistant can explain itself in-thread.

``message.im`` (DM)      → established DM route wins; otherwise the
    coordinator handles initial / unknown DMs.

``message.channels``     → an established thread route wins; otherwise
    the channel binding wins; otherwise we return ``None`` (the gateway
    is expected to drop the event because nothing is bound).

Side effects are limited to upserting / touching thread routes (so the
caller doesn't have to re-derive the routing key).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.models.orchestra_models import (
    DM_ROOT_SENTINEL,
    Assistant,
    SlackInstall,
    User,
)

logger = logging.getLogger(__name__)


# ``<@U01ABC123>`` followed by optional whitespace then the user-visible
# remainder of the message. The mention must start the trimmed text, so we
# anchor at the start.
_BOT_MENTION_RE = re.compile(
    r"^\s*<@(?P<user_id>[UW][A-Z0-9]+)>\s*(?P<rest>.*)$",
    re.DOTALL,
)

# Punctuation commonly attached to an addressing token when a boss types
# naturally, e.g. ``@app Lily, …`` or ``@app Lily: …``. Stripped from each
# word's edges before token resolution. Hyphen/apostrophe are intentionally
# excluded so names like ``Mary-Jane`` and ``O'Brien`` survive.
_TOKEN_EDGE_PUNCT = ',.;:!?"`()[]{}<>'


def _clean_word(word: str) -> str:
    """Strip surrounding punctuation from a single addressing word."""
    return word.strip(_TOKEN_EDGE_PUNCT)


@dataclass
class SenderIdentity:
    """Best-effort identity of the human who sent an inbound Slack event.

    The gateway populates this on the *second* dispatch pass — after a
    ``users.info`` lookup against the workspace bot token — so an org
    install can route an untokened DM or an ambiguous mention to the
    sender's *own* workspace Coordinator. On the first pass ``provided``
    is ``False``: the dispatcher routes provisionally to the org's
    deterministic Coordinator and asks the caller to re-dispatch with
    identity for per-member precision.
    """

    provided: bool = False
    email: Optional[str] = None
    real_name: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class SlackInboundResolution:
    """Outcome of routing a single inbound Slack event."""

    install: SlackInstall
    assistant_id: int
    """Final recipient. Always a real assistant row in the install's org."""

    thread_ts_for_route: str
    """Key used to upsert a :class:`SlackThreadRoute` if persisting.

    For channel messages this is the user-facing ``thread_ts`` (the
    message's own ``ts`` if it is itself the thread root). For DMs this
    is :data:`DM_ROOT_SENTINEL`.
    """

    route_persisted: bool = False
    """True when a thread/DM route was upserted (or touched) for this event."""

    routing_metadata: dict[str, Any] = field(default_factory=dict)
    """Structured hints surfaced to the receiving assistant.

    Examples:
        ``{"reason": "unknown_token", "token": "alx"}``
        ``{"reason": "ambiguous_token", "token": "alex", "candidates": [...]}``
        ``{"reason": "initial_dm"}``
        ``{"reason": "channel_binding"}``
    """

    needs_sender_identity: bool = False
    """True when this is a *provisional* coordinator route for an org
    install and the caller should re-dispatch with the sender's identity
    (email / name) so the message can be pinned to the sender's own
    workspace Coordinator. The provisional ``assistant_id`` is still a
    valid recipient, so a caller that ignores this flag degrades to
    deterministic org-Coordinator routing rather than dropping the event.
    """


def _extract_rest(text: str, bot_user_id: str) -> Optional[str]:
    """Return the message text following ``<@bot> ...`` or None.

    The mention must be the first non-whitespace element of the message,
    must reference *this* install's bot, and must be followed by a
    non-empty remainder.
    """
    if not text:
        return None
    m = _BOT_MENTION_RE.match(text)
    if m is None:
        return None
    if m.group("user_id") != bot_user_id:
        return None
    rest = m.group("rest").strip()
    if not rest:
        return None
    return rest


def _resolve_addressing(
    session: Session,
    install: SlackInstall,
    rest: str,
) -> tuple[list[Assistant], str]:
    """Resolve the words after a bot mention to candidate assistants.

    Addressing escalates from most- to least-specific so first-name
    collisions are still routable:

    1. **Full name** — the leading two words (``"first surname"``) are
       tried first; an unambiguous full-name hit wins immediately. This
       is the canonical dedup form when several assistants share a first
       name.
    2. **Leading word** — otherwise the single first word is resolved,
       which :meth:`AssistantDAO.resolve_token` matches against the
       numeric agent id or the first name.

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


def _scope_kwargs(install: SlackInstall) -> dict[str, Any]:
    """Translate an install's polymorphic owner into AssistantDAO kwargs.

    ``SlackInstall`` always has exactly one of ``organization_id`` /
    ``user_id`` set (enforced by ``ck_slack_install_one_owner``). The
    assistant DAO accepts the same XOR, so we just forward whichever
    field is populated.
    """
    if install.organization_id is not None:
        return {"organization_id": install.organization_id}
    return {"user_id": install.user_id}


def resolve_inbound(
    session: Session,
    *,
    slack_team_id: str,
    channel_id: str,
    channel_type: str,
    sender_slack_user_id: str,
    text: str,
    thread_ts: Optional[str],
    event_ts: str,
    sender_email: Optional[str] = None,
    sender_real_name: Optional[str] = None,
    sender_display_name: Optional[str] = None,
    sender_identity_provided: bool = False,
) -> Optional[SlackInboundResolution]:
    """Route a single inbound Slack event.

    :param channel_type: ``"im"`` for direct messages, ``"channel"`` /
        ``"group"`` / ``"mpim"`` for everything else.
    :param thread_ts: Slack thread root timestamp if the event is a
        threaded reply, else ``None``.
    :param event_ts: The event's own ``ts``. Used as the route key when
        the event is itself the thread root (i.e. the first explicit
        ``@app <token>`` in a top-level message that starts a thread).
    :param sender_email: Sender's Slack profile email, if the gateway has
        resolved it (org installs, second dispatch pass).
    :param sender_real_name: Sender's Slack ``real_name``, if resolved.
    :param sender_display_name: Sender's Slack ``display_name``, if resolved.
    :param sender_identity_provided: ``True`` once the gateway has attempted
        a ``users.info`` lookup (even if it yielded nothing). Distinguishes
        the first dispatch pass from the second so coordinator routing does
        not loop asking for identity that cannot be obtained.

    Returns ``None`` when no assistant should receive the event (e.g.
    untokened message in a channel with no binding, or an install with no
    Coordinator).
    """
    slack_dao = SlackDAO(session)
    install = slack_dao.get_install_by_team(slack_team_id)
    if install is None:
        return None

    if sender_slack_user_id == install.bot_user_id:
        # Echoes of the bot's own messages — never route back.
        return None

    identity = SenderIdentity(
        provided=sender_identity_provided,
        email=sender_email,
        real_name=sender_real_name,
        display_name=sender_display_name,
    )

    is_dm = channel_type == "im"
    rest = _extract_rest(text, install.bot_user_id)

    if is_dm:
        return _resolve_dm(
            session=session,
            slack_dao=slack_dao,
            install=install,
            channel_id=channel_id,
            rest=rest,
            identity=identity,
        )

    return _resolve_channel(
        session=session,
        slack_dao=slack_dao,
        install=install,
        channel_id=channel_id,
        rest=rest,
        thread_ts=thread_ts,
        event_ts=event_ts,
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
    """Users who own a Coordinator assistant in this organization.

    These are the only members a sender can be pinned to by identity:
    each holds exactly one workspace Coordinator (unique per the
    ``(user_id, organization_id)`` membership index), so resolving the
    sender to one of them yields an unambiguous Coordinator.
    """
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
    """Map a Slack sender to the org member who owns their Coordinator.

    Mirrors the contact-matching ladder used for inbound senders: an exact
    email match wins, otherwise a unique full-name match (either ordering)
    on the member's profile name. Ambiguous name matches are refused so we
    never route to the wrong person.
    """
    owners = _coordinator_owners(session, organization_id)
    if not owners:
        return None

    email = (identity.email or "").strip().lower()
    if email:
        for owner in owners:
            if (owner.email or "").strip().lower() == email:
                return owner.id

    for raw_name in (identity.real_name, identity.display_name):
        target = _normalize_name(raw_name or "")
        if not target:
            continue
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
    install: SlackInstall,
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

    A ``None`` coordinator means the install has none at all; the caller
    drops the event rather than crashing.
    """
    dao = AssistantDAO(session)
    if install.organization_id is None:
        return dao.coordinator(user_id=install.user_id), False

    organization_id = install.organization_id
    if identity.provided:
        member_user_id = _resolve_member_user_id(session, organization_id, identity)
        if member_user_id is not None:
            member_coordinator = dao.coordinator(
                user_id=member_user_id,
                organization_id=organization_id,
            )
            if member_coordinator is not None:
                return member_coordinator, False
        return dao.coordinator(organization_id=organization_id), False

    return dao.coordinator(organization_id=organization_id), True


def _resolve_dm(
    *,
    session: Session,
    slack_dao: SlackDAO,
    install: SlackInstall,
    channel_id: str,
    rest: Optional[str],
    identity: SenderIdentity,
) -> Optional[SlackInboundResolution]:
    if rest is not None:
        candidates, token = _resolve_addressing(session, install, rest)
        if len(candidates) == 1:
            assistant = candidates[0]
            slack_dao.upsert_dm_route(install.id, channel_id, assistant.agent_id)
            return SlackInboundResolution(
                install=install,
                assistant_id=assistant.agent_id,
                thread_ts_for_route=DM_ROOT_SENTINEL,
                route_persisted=True,
                routing_metadata={"reason": "token_addressed", "token": token},
            )

        coordinator, needs_identity = _resolve_coordinator(session, install, identity)
        if coordinator is None:
            return None
        meta: dict[str, Any] = {
            "reason": "unknown_token" if not candidates else "ambiguous_token",
            "token": token,
        }
        if candidates:
            meta["candidates"] = [_assistant_label(a) for a in candidates]
        if not needs_identity:
            # Final coordinator route: pin the DM so follow-ups skip the
            # (potentially Slack-API-backed) identity lookup.
            slack_dao.upsert_dm_route(install.id, channel_id, coordinator.agent_id)
        return SlackInboundResolution(
            install=install,
            assistant_id=coordinator.agent_id,
            thread_ts_for_route=DM_ROOT_SENTINEL,
            route_persisted=not needs_identity,
            routing_metadata=meta,
            needs_sender_identity=needs_identity,
        )

    existing = slack_dao.get_dm_route(install.id, channel_id)
    if existing is not None:
        slack_dao.touch_thread_route(existing)
        return SlackInboundResolution(
            install=install,
            assistant_id=existing.assistant_id,
            thread_ts_for_route=DM_ROOT_SENTINEL,
            route_persisted=True,
        )

    coordinator, needs_identity = _resolve_coordinator(session, install, identity)
    if coordinator is None:
        return None
    if not needs_identity:
        slack_dao.upsert_dm_route(install.id, channel_id, coordinator.agent_id)
    return SlackInboundResolution(
        install=install,
        assistant_id=coordinator.agent_id,
        thread_ts_for_route=DM_ROOT_SENTINEL,
        route_persisted=not needs_identity,
        routing_metadata={"reason": "initial_dm"},
        needs_sender_identity=needs_identity,
    )


def _resolve_channel(
    *,
    session: Session,
    slack_dao: SlackDAO,
    install: SlackInstall,
    channel_id: str,
    rest: Optional[str],
    thread_ts: Optional[str],
    event_ts: str,
    identity: SenderIdentity,
) -> Optional[SlackInboundResolution]:
    # The thread root we use as the route key. If the event is itself
    # a top-level message that starts a new thread (via an explicit
    # ``@app <token>``), Slack still gives us ``thread_ts is None``;
    # downstream replies will carry ``thread_ts == event_ts``.
    route_key = thread_ts or event_ts

    if rest is not None:
        candidates, token = _resolve_addressing(session, install, rest)
        if len(candidates) == 1:
            assistant = candidates[0]
            slack_dao.upsert_thread_route(
                install.id,
                channel_id,
                route_key,
                assistant.agent_id,
            )
            return SlackInboundResolution(
                install=install,
                assistant_id=assistant.agent_id,
                thread_ts_for_route=route_key,
                route_persisted=True,
                routing_metadata={"reason": "token_addressed", "token": token},
            )

        coordinator, needs_identity = _resolve_coordinator(session, install, identity)
        if coordinator is None:
            return None
        # The coordinator takes the thread so it can clarify in place. A
        # provisional (needs-identity) route is still persisted; a second
        # dispatch with the sender's identity re-pins it to the sender's
        # own workspace Coordinator if that differs.
        slack_dao.upsert_thread_route(
            install.id,
            channel_id,
            route_key,
            coordinator.agent_id,
        )
        meta: dict[str, Any] = {
            "reason": "unknown_token" if not candidates else "ambiguous_token",
            "token": token,
        }
        if candidates:
            meta["candidates"] = [_assistant_label(a) for a in candidates]
        return SlackInboundResolution(
            install=install,
            assistant_id=coordinator.agent_id,
            thread_ts_for_route=route_key,
            route_persisted=True,
            routing_metadata=meta,
            needs_sender_identity=needs_identity,
        )

    if thread_ts is not None:
        existing = slack_dao.get_thread_route(install.id, channel_id, thread_ts)
        if existing is not None:
            slack_dao.touch_thread_route(existing)
            return SlackInboundResolution(
                install=install,
                assistant_id=existing.assistant_id,
                thread_ts_for_route=thread_ts,
                route_persisted=True,
            )
        # In-thread reply with no token and no live route — fall through
        # to the binding check, but only if there is one.

    binding = slack_dao.get_channel_binding(install.id, channel_id)
    if binding is None:
        # Nothing addresses this assistant; gateway should drop the event.
        return None

    return SlackInboundResolution(
        install=install,
        assistant_id=binding.assistant_id,
        thread_ts_for_route=route_key,
        route_persisted=False,
        routing_metadata={"reason": "channel_binding"},
    )
