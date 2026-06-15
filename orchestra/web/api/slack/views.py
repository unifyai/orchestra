"""Admin endpoints for Slack per-workspace OAuth installs.

Called by the Unity gateway (``unity/gateway/channels/slack``) for:

* Persisting an install after the OAuth callback completes.
* Resolving each inbound event to an assistant.
* Looking up the install (and bot token) when sending outbound.
* Upserting a thread route immediately after an outbound send so that
  in-thread replies return to the same assistant.

Pure persistence — all routing logic lives in
``orchestra.services.slack_dispatcher``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.slack_dao import DEFAULT_THREAD_ROUTE_TTL_DAYS, SlackDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import SlackInstall
from orchestra.services.slack_dispatcher import resolve_inbound

admin_router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class InstallUpsertRequest(BaseModel):
    """Body for the install upsert endpoint.

    Exactly one of ``organization_id`` or ``user_id`` must be set —
    the install is owned by either a Unify organization or a personal
    Unify user, never both.
    """

    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    slack_team_id: str
    slack_app_id: str
    bot_user_id: str
    bot_access_token: str
    slack_team_name: Optional[str] = None
    enterprise_id: Optional[str] = None
    installer_user_id: Optional[str] = None
    scopes: Optional[str] = None


class InstallResponse(BaseModel):
    id: int
    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    slack_team_id: str
    slack_team_name: Optional[str] = None
    slack_app_id: str
    enterprise_id: Optional[str] = None
    bot_user_id: str
    installer_user_id: Optional[str] = None
    scopes: Optional[str] = None
    revoked: bool = False
    bot_access_token: Optional[str] = Field(
        None,
        description=(
            "Bot token. Only populated when ``include_token=true`` is "
            "passed on read endpoints; never returned by list endpoints."
        ),
    )


class DispatchRequest(BaseModel):
    slack_team_id: str
    channel_id: str
    channel_type: str = Field(
        ...,
        description="'im' for direct messages, 'channel'/'group'/'mpim' for others.",
    )
    sender_slack_user_id: str
    text: str
    thread_ts: Optional[str] = None
    event_ts: str


class DispatchResponse(BaseModel):
    handled: bool = Field(
        ...,
        description=(
            "True if an assistant was found for this event. False when no "
            "install exists, the event is a bot echo, or the channel is "
            "unbound and has no live thread route."
        ),
    )
    install_id: Optional[int] = None
    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    bot_user_id: Optional[str] = None
    thread_ts_for_route: Optional[str] = None
    route_persisted: bool = False
    routing_metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelBindingRequest(BaseModel):
    install_id: int
    channel_id: str
    assistant_id: int
    channel_name: Optional[str] = None


class ChannelBindingResponse(BaseModel):
    id: int
    install_id: int
    channel_id: str
    channel_name: Optional[str] = None
    assistant_id: int


class ThreadRouteUpsertRequest(BaseModel):
    install_id: int
    channel_id: str
    thread_ts: str = Field(
        ...,
        description=(
            "Either the Slack thread root ts or the sentinel "
            "``__dm_root__`` for DM-root routes."
        ),
    )
    assistant_id: int
    ttl_days: int = DEFAULT_THREAD_ROUTE_TTL_DAYS


class ThreadRouteResponse(BaseModel):
    id: int
    install_id: int
    channel_id: str
    thread_ts: str
    assistant_id: int
    expires_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_to_response(
    install: SlackInstall,
    *,
    include_token: bool = False,
) -> InstallResponse:
    return InstallResponse(
        id=install.id,
        organization_id=install.organization_id,
        user_id=install.user_id,
        slack_team_id=install.slack_team_id,
        slack_team_name=install.slack_team_name,
        slack_app_id=install.slack_app_id,
        enterprise_id=install.enterprise_id,
        bot_user_id=install.bot_user_id,
        installer_user_id=install.installer_user_id,
        scopes=install.scopes,
        revoked=install.revoked_at is not None,
        bot_access_token=install.bot_access_token if include_token else None,
    )


def _require_owner_xor(
    organization_id: Optional[int],
    user_id: Optional[str],
) -> None:
    """Validate at the HTTP boundary so we return a clean 400."""
    has_org = organization_id is not None
    has_user = user_id is not None
    if has_org == has_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Provide exactly one of organization_id or user_id "
                "(got organization_id="
                f"{organization_id!r}, user_id={user_id!r})."
            ),
        )


# ---------------------------------------------------------------------------
# Installs
# ---------------------------------------------------------------------------


@admin_router.post("/slack/install")
def upsert_install(
    body: InstallUpsertRequest,
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Create or refresh a Slack install for an owner (org or user).

    Called by the gateway after the OAuth callback exchanges the code
    for a bot token. Idempotent on ``(owner, slack_team_id)``; a
    re-install replaces the bot token and clears any prior ``revoked_at``.
    """
    _require_owner_xor(body.organization_id, body.user_id)
    dao = SlackDAO(session)
    install = dao.upsert_install(
        organization_id=body.organization_id,
        user_id=body.user_id,
        slack_team_id=body.slack_team_id,
        slack_app_id=body.slack_app_id,
        bot_user_id=body.bot_user_id,
        bot_access_token=body.bot_access_token,
        slack_team_name=body.slack_team_name,
        enterprise_id=body.enterprise_id,
        installer_user_id=body.installer_user_id,
        scopes=body.scopes,
    )
    session.commit()
    return _install_to_response(install)


@admin_router.get("/slack/install")
def get_install(
    slack_team_id: Optional[str] = Query(
        None,
        description="Resolve install by Slack workspace id.",
    ),
    organization_id: Optional[int] = Query(
        None,
        description="Resolve install by Unify organization id.",
    ),
    user_id: Optional[str] = Query(
        None,
        description="Resolve install by personal Unify user id.",
    ),
    include_token: bool = Query(
        False,
        description="Include the bot access token (admin-auth gated).",
    ),
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Look up a Slack install by workspace, org, or personal user."""
    selectors = [
        ("slack_team_id", slack_team_id),
        ("organization_id", organization_id),
        ("user_id", user_id),
    ]
    populated = [name for name, val in selectors if val is not None]
    if len(populated) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Provide exactly one of slack_team_id, organization_id, "
                f"or user_id (got {populated})."
            ),
        )
    dao = SlackDAO(session)
    if slack_team_id is not None:
        install = dao.get_install_by_team(slack_team_id)
    elif organization_id is not None:
        install = dao.get_install_for_org(organization_id)
    else:
        install = dao.get_install_for_user(user_id)  # type: ignore[arg-type]
    if install is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active Slack install found.",
        )
    return _install_to_response(install, include_token=include_token)


@admin_router.delete("/slack/install/{install_id}")
def revoke_install(
    install_id: int,
    session: Session = Depends(get_db_session),
):
    """Mark a Slack install as revoked and drop its routing state."""
    dao = SlackDAO(session)
    install = dao.revoke_install(install_id)
    if install is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Slack install not found.",
        )
    session.commit()
    return {"id": install.id, "revoked": True}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@admin_router.post("/slack/dispatch")
def dispatch_inbound(
    body: DispatchRequest,
    session: Session = Depends(get_db_session),
) -> DispatchResponse:
    """Resolve an inbound Slack event to an assistant.

    The gateway calls this once per webhook after HMAC verification.
    The response carries everything the gateway needs to fan the event
    out to the assistant's Pub/Sub topic and tag it with structured
    routing metadata.
    """
    resolution = resolve_inbound(
        session,
        slack_team_id=body.slack_team_id,
        channel_id=body.channel_id,
        channel_type=body.channel_type,
        sender_slack_user_id=body.sender_slack_user_id,
        text=body.text,
        thread_ts=body.thread_ts,
        event_ts=body.event_ts,
    )
    if resolution is None:
        session.commit()
        return DispatchResponse(handled=False)

    session.commit()
    return DispatchResponse(
        handled=True,
        install_id=resolution.install.id,
        organization_id=resolution.install.organization_id,
        user_id=resolution.install.user_id,
        assistant_id=resolution.assistant_id,
        bot_user_id=resolution.install.bot_user_id,
        thread_ts_for_route=resolution.thread_ts_for_route,
        route_persisted=resolution.route_persisted,
        routing_metadata=resolution.routing_metadata,
    )


# ---------------------------------------------------------------------------
# Channel bindings
# ---------------------------------------------------------------------------


@admin_router.post("/slack/channel-bindings")
def upsert_channel_binding(
    body: ChannelBindingRequest,
    session: Session = Depends(get_db_session),
) -> ChannelBindingResponse:
    """Bind (or rebind) a Slack channel to a default assistant."""
    dao = SlackDAO(session)
    binding = dao.bind_channel(
        install_id=body.install_id,
        channel_id=body.channel_id,
        assistant_id=body.assistant_id,
        channel_name=body.channel_name,
    )
    session.commit()
    return ChannelBindingResponse(
        id=binding.id,
        install_id=binding.install_id,
        channel_id=binding.channel_id,
        channel_name=binding.channel_name,
        assistant_id=binding.assistant_id,
    )


@admin_router.delete("/slack/channel-bindings")
def delete_channel_binding(
    install_id: int = Query(...),
    channel_id: str = Query(...),
    session: Session = Depends(get_db_session),
):
    """Remove a channel binding."""
    dao = SlackDAO(session)
    deleted = dao.unbind_channel(install_id, channel_id)
    session.commit()
    return {"deleted": deleted}


@admin_router.get("/slack/channel-bindings")
def list_channel_bindings(
    install_id: int = Query(...),
    session: Session = Depends(get_db_session),
) -> list[ChannelBindingResponse]:
    """List all channel bindings for an install."""
    dao = SlackDAO(session)
    return [
        ChannelBindingResponse(
            id=b.id,
            install_id=b.install_id,
            channel_id=b.channel_id,
            channel_name=b.channel_name,
            assistant_id=b.assistant_id,
        )
        for b in dao.list_channel_bindings(install_id)
    ]


# ---------------------------------------------------------------------------
# Thread routes (channel threads + DM roots)
# ---------------------------------------------------------------------------


@admin_router.post("/slack/thread-routes")
def upsert_thread_route(
    body: ThreadRouteUpsertRequest,
    session: Session = Depends(get_db_session),
) -> ThreadRouteResponse:
    """Pin a conversation to an assistant.

    Called by the gateway immediately after an outbound send so that
    subsequent in-thread replies (or DM replies) return to the
    initiating assistant. Idempotent — refreshes ``last_used_at`` and
    ``expires_at`` on repeat calls.
    """
    dao = SlackDAO(session)
    route = dao.upsert_thread_route(
        install_id=body.install_id,
        channel_id=body.channel_id,
        thread_ts=body.thread_ts,
        assistant_id=body.assistant_id,
        ttl_days=body.ttl_days,
    )
    session.commit()
    return ThreadRouteResponse(
        id=route.id,
        install_id=route.install_id,
        channel_id=route.channel_id,
        thread_ts=route.thread_ts,
        assistant_id=route.assistant_id,
        expires_at=route.expires_at.isoformat() if route.expires_at else None,
    )


@admin_router.post("/slack/thread-routes/prune")
def prune_expired_thread_routes(
    session: Session = Depends(get_db_session),
):
    """Delete all expired thread routes. Intended for a daily cron job."""
    dao = SlackDAO(session)
    count = dao.delete_expired_routes()
    session.commit()
    return {"deleted": count}
