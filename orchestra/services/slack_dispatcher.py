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
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.models.orchestra_models import (
    DM_ROOT_SENTINEL,
    Assistant,
    SlackInstall,
)

logger = logging.getLogger(__name__)


# ``<@U01ABC123>`` followed by optional whitespace then the user-visible
# remainder of the message. The mention must start the trimmed text, so we
# anchor at the start.
_BOT_MENTION_RE = re.compile(
    r"^\s*<@(?P<user_id>[UW][A-Z0-9]+)>\s*(?P<rest>.*)$",
    re.DOTALL,
)


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
    words = rest.split()
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
) -> Optional[SlackInboundResolution]:
    """Route a single inbound Slack event.

    :param channel_type: ``"im"`` for direct messages, ``"channel"`` /
        ``"group"`` / ``"mpim"`` for everything else.
    :param thread_ts: Slack thread root timestamp if the event is a
        threaded reply, else ``None``.
    :param event_ts: The event's own ``ts``. Used as the route key when
        the event is itself the thread root (i.e. the first explicit
        ``@app <token>`` in a top-level message that starts a thread).

    Returns ``None`` when no assistant should receive the event (e.g.
    untokened message in a channel with no binding).
    """
    slack_dao = SlackDAO(session)
    install = slack_dao.get_install_by_team(slack_team_id)
    if install is None:
        return None

    if sender_slack_user_id == install.bot_user_id:
        # Echoes of the bot's own messages — never route back.
        return None

    is_dm = channel_type == "im"
    rest = _extract_rest(text, install.bot_user_id)

    if is_dm:
        return _resolve_dm(
            session=session,
            slack_dao=slack_dao,
            install=install,
            channel_id=channel_id,
            rest=rest,
        )

    return _resolve_channel(
        session=session,
        slack_dao=slack_dao,
        install=install,
        channel_id=channel_id,
        rest=rest,
        thread_ts=thread_ts,
        event_ts=event_ts,
    )


def _coordinator_or_fail(
    session: Session,
    install: SlackInstall,
) -> Assistant:
    coordinator = AssistantDAO(session).coordinator(**_scope_kwargs(install))
    if coordinator is None:
        # The install was provisioned without an owner-scope coordinator.
        # The OAuth flow is responsible for ensuring one exists; if it
        # doesn't, the dispatcher cannot route untokened or ambiguous
        # traffic and we should fail loudly.
        owner = (
            f"org {install.organization_id}"
            if install.organization_id is not None
            else f"user {install.user_id!r}"
        )
        raise RuntimeError(
            f"Slack install {install.id} ({owner}) has no Coordinator "
            "assistant; cannot route ambiguous traffic.",
        )
    return coordinator


def _resolve_dm(
    *,
    session: Session,
    slack_dao: SlackDAO,
    install: SlackInstall,
    channel_id: str,
    rest: Optional[str],
) -> SlackInboundResolution:
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

        coordinator = _coordinator_or_fail(session, install)
        meta: dict[str, Any] = {
            "reason": "unknown_token" if not candidates else "ambiguous_token",
            "token": token,
        }
        if candidates:
            meta["candidates"] = [_assistant_label(a) for a in candidates]
        return SlackInboundResolution(
            install=install,
            assistant_id=coordinator.agent_id,
            thread_ts_for_route=DM_ROOT_SENTINEL,
            route_persisted=False,
            routing_metadata=meta,
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

    coordinator = _coordinator_or_fail(session, install)
    return SlackInboundResolution(
        install=install,
        assistant_id=coordinator.agent_id,
        thread_ts_for_route=DM_ROOT_SENTINEL,
        route_persisted=False,
        routing_metadata={"reason": "initial_dm"},
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

        coordinator = _coordinator_or_fail(session, install)
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
