"""Admin endpoints for the multi-tenant MS Teams (Bot Framework) bot.

Called by the Unify gateway / hosted adapter for:

* Recording a *pending* install on the first ``conversationUpdate`` from a
  Teams Store install, then binding it to a Unify owner via the
  tenant-to-org handshake.
* Persisting an already-owned install (Console-driven path).
* Resolving each inbound activity to an assistant.
* Looking up the install (``service_url`` / ``bot_app_id``) and the
  conversation reference when sending outbound proactively.
* Upserting a conversation route immediately after an outbound send so
  replies in the same conversation return to the same assistant.

Pure persistence — all routing logic lives in
``orchestra.services.ms_teams_bot_dispatcher``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.ms_teams_bot_dao import (
    DEFAULT_CONVERSATION_ROUTE_TTL_DAYS,
    MsTeamsBotDAO,
)
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import MsTeamsBotInstall
from orchestra.services.ms_teams_bot_dispatcher import resolve_inbound
from orchestra.settings import settings

admin_router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PendingInstallRequest(BaseModel):
    """Record a Teams Store install before its Unify owner is known.

    Created on the first ``conversationUpdate`` (bot added to a tenant).
    Returns the ``bind_nonce`` used to complete the tenant-to-org
    handshake.
    """

    tenant_id: str
    bot_app_id: str
    tenant_name: Optional[str] = None
    service_url: Optional[str] = None
    installer_aad_object_id: Optional[str] = None


class InstallUpsertRequest(BaseModel):
    """Body for the owned-install upsert endpoint.

    At most one of ``organization_id`` or ``user_id`` — the Console path
    always sets exactly one (the owner is known up front).
    """

    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    tenant_id: str
    bot_app_id: str
    tenant_name: Optional[str] = None
    service_url: Optional[str] = None
    installer_aad_object_id: Optional[str] = None
    scopes: Optional[str] = None


class BindInstallRequest(BaseModel):
    """Bind a pending install to a Unify owner (exactly one of org/user)."""

    install_id: int
    organization_id: Optional[int] = None
    user_id: Optional[str] = None


class InstallResponse(BaseModel):
    id: int
    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    tenant_id: str
    tenant_name: Optional[str] = None
    bot_app_id: str
    service_url: Optional[str] = None
    installer_aad_object_id: Optional[str] = None
    scopes: Optional[str] = None
    pending: bool = False
    revoked: bool = False
    created: bool = Field(
        False,
        description=(
            "True only when the pending-install call inserted a brand-new row. "
            "The adapter sends the install welcome exactly once off this flag — "
            "a Teams add fires both an installationUpdate and a "
            "conversationUpdate, so welcoming on every event would spam. Only "
            "meaningful on the pending-install response."
        ),
    )
    bind_nonce: Optional[str] = Field(
        None,
        description=(
            "Handshake nonce for a pending install. Only populated when "
            "``include_nonce=true`` is passed; never returned by list "
            "endpoints."
        ),
    )
    connect_url: Optional[str] = Field(
        None,
        description=(
            "One-click Console URL that claims this pending install for the "
            "signed-in owner (carries ``bind_nonce`` as the ``nonce`` query "
            "param). Populated alongside ``bind_nonce`` so the bot can DM the "
            "installer a single link instead of a code to copy."
        ),
    )


class WelcomeClaimRequest(BaseModel):
    install_id: int
    conversation_id: str


class WelcomeClaimResponse(BaseModel):
    claimed: bool = Field(
        ...,
        description=(
            "True only when this call recorded the welcome for the "
            "conversation — the adapter should send the greeting now. False "
            "when the conversation was already welcomed (a redelivered "
            "bot-add), so the adapter must stay silent to avoid repeating the "
            "welcome."
        ),
    )


class DispatchRequest(BaseModel):
    tenant_id: str
    conversation_id: str
    conversation_type: str = Field(
        ...,
        description="'personal' for 1:1 chats, 'groupChat', or 'channel'.",
    )
    channel_id: Optional[str] = Field(
        None,
        description="channelData.channel.id for channel activities, else null.",
    )
    sender_aad_object_id: str
    bot_mentioned: bool
    addressed_text: str = Field(
        ...,
        description="Message text with the bot <at> mention already stripped.",
    )
    conversation_reference: Optional[str] = Field(
        None,
        description="Serialized Bot Framework ConversationReference (JSON).",
    )
    sender_email: Optional[str] = Field(
        None,
        description="Sender email, resolved by the adapter on the second pass.",
    )
    sender_display_name: Optional[str] = Field(
        None,
        description="Sender AAD display name (from.name).",
    )
    sender_identity_provided: bool = Field(
        False,
        description="True once the adapter has attempted the identity lookup.",
    )


class DispatchResponse(BaseModel):
    handled: bool = Field(
        ...,
        description=(
            "True if an assistant was found. False when no install exists, "
            "the install is pending (unbound), the activity is a bot echo, "
            "or the owner has no Coordinator."
        ),
    )
    install_id: Optional[int] = None
    organization_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    bot_app_id: Optional[str] = None
    service_url: Optional[str] = None
    conversation_id_for_route: Optional[str] = None
    route_persisted: bool = False
    routing_metadata: dict[str, Any] = Field(default_factory=dict)
    needs_sender_identity: bool = Field(
        False,
        description=(
            "True when this is a provisional org-Coordinator route and the "
            "adapter should resolve the sender's email (roster / Graph) and "
            "re-dispatch with sender_identity_provided=True so the activity "
            "is pinned to the sender's own workspace Coordinator."
        ),
    )
    sender_is_owner: bool = Field(
        False,
        description=(
            "True when the sender is the human owner (boss) of the resolved "
            "assistant. The runtime attributes the message to the durable boss "
            "contact instead of minting a per-display-name Teams contact."
        ),
    )
    install_state: str = Field(
        "none",
        description=(
            "Owner-binding state of the tenant's install for this activity: "
            "'none' (no install), 'pending' (installed but not yet bound to a "
            "Unify owner), or 'bound'. Lets the adapter reply to a message that "
            "lands on a still-pending install with a connect link rather than "
            "dropping it silently."
        ),
    )
    connect_url: Optional[str] = Field(
        None,
        description=(
            "One-click Console URL to bind a pending install (carries the "
            "``bind_nonce`` as the ``nonce`` query param). Populated only when "
            "``install_state == 'pending'``."
        ),
    )


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


class ConversationRouteUpsertRequest(BaseModel):
    install_id: int
    conversation_id: str
    assistant_id: int
    conversation_reference: Optional[str] = None
    ttl_days: int = DEFAULT_CONVERSATION_ROUTE_TTL_DAYS


class ConversationRouteResponse(BaseModel):
    id: int
    install_id: int
    conversation_id: str
    assistant_id: int
    conversation_reference: Optional[str] = None
    expires_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_connect_url(nonce: str) -> str:
    """One-click Console link that binds this install to the signed-in owner.

    Console's ``/connect/ms-teams`` route handler claims the install server-side
    and then redirects to the assistants surface, so the installer never copies a
    code by hand. It is a server route rather than a page param because the link
    is usually opened without a Console session: the request funnels through
    sign-in (and possibly MFA / account onboarding) first, and only Console's
    middleware can carry the nonce across those hops.
    """
    base = (settings.console_url or "https://console.unify.ai/").rstrip("/")
    return f"{base}/connect/ms-teams?nonce={quote(nonce)}"


def _install_to_response(
    install: MsTeamsBotInstall,
    *,
    include_nonce: bool = False,
    created: bool = False,
) -> InstallResponse:
    pending = install.organization_id is None and install.user_id is None
    nonce = install.bind_nonce if include_nonce else None
    return InstallResponse(
        id=install.id,
        organization_id=install.organization_id,
        user_id=install.user_id,
        tenant_id=install.tenant_id,
        tenant_name=install.tenant_name,
        bot_app_id=install.bot_app_id,
        service_url=install.service_url,
        installer_aad_object_id=install.installer_aad_object_id,
        scopes=install.scopes,
        pending=pending,
        revoked=install.revoked_at is not None,
        created=created,
        bind_nonce=nonce,
        connect_url=_build_connect_url(nonce) if nonce else None,
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


@admin_router.get("/ms-teams-bot/installs")
def list_installs(
    session: Session = Depends(get_db_session),
) -> list[InstallResponse]:
    """List every MS Teams bot install known to Orchestra (debug / health)."""
    installs = MsTeamsBotDAO(session).list_installs()
    return [_install_to_response(install) for install in installs]


@admin_router.post("/ms-teams-bot/pending-install")
def ensure_pending_install(
    body: PendingInstallRequest,
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Record a pending (unbound) install for a Microsoft tenant.

    Idempotent on the tenant: an existing active install (pending or bound)
    is refreshed and returned. A freshly created row carries a
    ``bind_nonce`` (returned here) for the tenant-to-org handshake.
    """
    dao = MsTeamsBotDAO(session)
    install, created = dao.ensure_pending_install(
        tenant_id=body.tenant_id,
        bot_app_id=body.bot_app_id,
        tenant_name=body.tenant_name,
        service_url=body.service_url,
        installer_aad_object_id=body.installer_aad_object_id,
    )
    session.commit()
    return _install_to_response(install, include_nonce=True, created=created)


@admin_router.post("/ms-teams-bot/bind")
def bind_install(
    body: BindInstallRequest,
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Bind a pending install to a Unify owner (org XOR user)."""
    _require_owner_xor(body.organization_id, body.user_id)
    dao = MsTeamsBotDAO(session)
    try:
        install = dao.bind_install(
            body.install_id,
            organization_id=body.organization_id,
            user_id=body.user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    session.commit()
    return _install_to_response(install)


@admin_router.post("/ms-teams-bot/install")
def upsert_install(
    body: InstallUpsertRequest,
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Create or refresh an already-owned MS Teams bot install.

    Console-driven path where the owner is known. Idempotent on
    ``(owner, tenant_id)``; a re-install refreshes ``service_url`` and
    clears any prior ``revoked_at``.
    """
    _require_owner_xor(body.organization_id, body.user_id)
    dao = MsTeamsBotDAO(session)
    install = dao.upsert_install(
        organization_id=body.organization_id,
        user_id=body.user_id,
        tenant_id=body.tenant_id,
        bot_app_id=body.bot_app_id,
        tenant_name=body.tenant_name,
        service_url=body.service_url,
        installer_aad_object_id=body.installer_aad_object_id,
        scopes=body.scopes,
    )
    session.commit()
    return _install_to_response(install)


@admin_router.get("/ms-teams-bot/install")
def get_install(
    tenant_id: Optional[str] = Query(
        None,
        description="Resolve install by Microsoft tenant id.",
    ),
    organization_id: Optional[int] = Query(
        None,
        description="Resolve install by Unify organization id.",
    ),
    user_id: Optional[str] = Query(
        None,
        description="Resolve install by personal Unify user id.",
    ),
    bind_nonce: Optional[str] = Query(
        None,
        description="Resolve a pending install by its handshake nonce.",
    ),
    include_nonce: bool = Query(
        False,
        description="Include the bind nonce (admin-auth gated).",
    ),
    session: Session = Depends(get_db_session),
) -> InstallResponse:
    """Look up an install by tenant, org, personal user, or bind nonce."""
    selectors = [
        ("tenant_id", tenant_id),
        ("organization_id", organization_id),
        ("user_id", user_id),
        ("bind_nonce", bind_nonce),
    ]
    populated = [name for name, val in selectors if val is not None]
    if len(populated) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Provide exactly one of tenant_id, organization_id, "
                f"user_id, or bind_nonce (got {populated})."
            ),
        )
    dao = MsTeamsBotDAO(session)
    if tenant_id is not None:
        install = dao.get_install_by_tenant(tenant_id)
    elif organization_id is not None:
        install = dao.get_install_for_org(organization_id)
    elif user_id is not None:
        install = dao.get_install_for_user(user_id)
    else:
        install = dao.get_install_by_nonce(bind_nonce)  # type: ignore[arg-type]
    if install is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active MS Teams bot install found.",
        )
    return _install_to_response(install, include_nonce=include_nonce)


@admin_router.delete("/ms-teams-bot/install/{install_id}")
def revoke_install(
    install_id: int,
    session: Session = Depends(get_db_session),
):
    """Mark an install as revoked and drop its routing state."""
    dao = MsTeamsBotDAO(session)
    install = dao.revoke_install(install_id)
    if install is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MS Teams bot install not found.",
        )
    session.commit()
    return {"id": install.id, "revoked": True}


# ---------------------------------------------------------------------------
# Welcome claims
# ---------------------------------------------------------------------------


@admin_router.post("/ms-teams-bot/welcome-claim")
def claim_welcome(
    body: WelcomeClaimRequest,
    session: Session = Depends(get_db_session),
) -> WelcomeClaimResponse:
    """Claim the one-shot install welcome for a Teams conversation.

    Idempotent per ``(install_id, conversation_id)``: the first caller wins
    with ``claimed=True`` and sends the greeting; redelivered bot-add events
    get ``claimed=False`` and stay silent. This is what keeps the bot from
    spamming repeated welcome messages when Teams / the Bot Connector
    redelivers the same bot-add ``conversationUpdate``.
    """
    dao = MsTeamsBotDAO(session)
    claimed = dao.claim_welcome(body.install_id, body.conversation_id)
    session.commit()
    return WelcomeClaimResponse(claimed=claimed)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@admin_router.post("/ms-teams-bot/dispatch")
def dispatch_inbound(
    body: DispatchRequest,
    session: Session = Depends(get_db_session),
) -> DispatchResponse:
    """Resolve an inbound Teams activity to an assistant.

    The adapter calls this once per activity after JWT verification. The
    response carries everything needed to fan the activity out to the
    assistant's Pub/Sub topic and to send the proactive reply.
    """
    try:
        resolution = resolve_inbound(
            session,
            tenant_id=body.tenant_id,
            conversation_id=body.conversation_id,
            conversation_type=body.conversation_type,
            channel_id=body.channel_id,
            sender_aad_object_id=body.sender_aad_object_id,
            bot_mentioned=body.bot_mentioned,
            addressed_text=body.addressed_text,
            conversation_reference=body.conversation_reference,
            sender_email=body.sender_email,
            sender_display_name=body.sender_display_name,
            sender_identity_provided=body.sender_identity_provided,
        )
        if resolution is None:
            # Nothing to route to. Tell the adapter *why* so it can reply to a
            # message on a still-pending install with a connect link (a common
            # Store-review path: reviewer messages the bot before binding).
            install = MsTeamsBotDAO(session).get_install_by_tenant(body.tenant_id)
            session.commit()
            if install is None:
                return DispatchResponse(handled=False, install_state="none")
            pending = install.organization_id is None and install.user_id is None
            if pending:
                return DispatchResponse(
                    handled=False,
                    install_state="pending",
                    install_id=install.id,
                    bot_app_id=install.bot_app_id,
                    service_url=install.service_url,
                    connect_url=(
                        _build_connect_url(install.bind_nonce)
                        if install.bind_nonce
                        else None
                    ),
                )
            return DispatchResponse(
                handled=False,
                install_state="bound",
                install_id=install.id,
            )

        session.commit()
        return DispatchResponse(
            handled=True,
            install_state="bound",
            install_id=resolution.install.id,
            organization_id=resolution.install.organization_id,
            user_id=resolution.install.user_id,
            assistant_id=resolution.assistant_id,
            bot_app_id=resolution.install.bot_app_id,
            service_url=resolution.install.service_url,
            conversation_id_for_route=resolution.conversation_id_for_route,
            route_persisted=resolution.route_persisted,
            routing_metadata=resolution.routing_metadata,
            needs_sender_identity=resolution.needs_sender_identity,
            sender_is_owner=resolution.sender_is_owner,
        )
    except Exception:
        # The Bot Connector retries non-2xx responses, so a routing fault
        # must never escape as a 500 — that turns a single transient error
        # into a redelivery storm. Log loud and tell the adapter to drop.
        logger.exception(
            "ms_teams_bot dispatch failed for tenant=%s conversation=%s; "
            "dropping activity",
            body.tenant_id,
            body.conversation_id,
        )
        session.rollback()
        return DispatchResponse(handled=False)


# ---------------------------------------------------------------------------
# Channel bindings
# ---------------------------------------------------------------------------


@admin_router.post("/ms-teams-bot/channel-bindings")
def upsert_channel_binding(
    body: ChannelBindingRequest,
    session: Session = Depends(get_db_session),
) -> ChannelBindingResponse:
    """Bind (or rebind) a Teams channel to a default assistant."""
    dao = MsTeamsBotDAO(session)
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


@admin_router.delete("/ms-teams-bot/channel-bindings")
def delete_channel_binding(
    install_id: int = Query(...),
    channel_id: str = Query(...),
    session: Session = Depends(get_db_session),
):
    """Remove a channel binding."""
    dao = MsTeamsBotDAO(session)
    deleted = dao.unbind_channel(install_id, channel_id)
    session.commit()
    return {"deleted": deleted}


@admin_router.get("/ms-teams-bot/channel-bindings")
def list_channel_bindings(
    install_id: int = Query(...),
    session: Session = Depends(get_db_session),
) -> list[ChannelBindingResponse]:
    """List all channel bindings for an install."""
    dao = MsTeamsBotDAO(session)
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
# Conversation routes
# ---------------------------------------------------------------------------


@admin_router.post("/ms-teams-bot/conversation-routes")
def upsert_conversation_route(
    body: ConversationRouteUpsertRequest,
    session: Session = Depends(get_db_session),
) -> ConversationRouteResponse:
    """Pin a conversation to an assistant.

    Called immediately after an outbound send so subsequent replies in the
    same conversation return to the initiating assistant. Idempotent —
    refreshes the stored conversation reference and TTL on repeat calls.
    """
    dao = MsTeamsBotDAO(session)
    route = dao.upsert_conversation_route(
        install_id=body.install_id,
        conversation_id=body.conversation_id,
        assistant_id=body.assistant_id,
        conversation_reference=body.conversation_reference,
        ttl_days=body.ttl_days,
    )
    session.commit()
    return ConversationRouteResponse(
        id=route.id,
        install_id=route.install_id,
        conversation_id=route.conversation_id,
        assistant_id=route.assistant_id,
        conversation_reference=route.conversation_reference,
        expires_at=route.expires_at.isoformat() if route.expires_at else None,
    )


@admin_router.get("/ms-teams-bot/conversation-routes")
def get_conversation_route(
    install_id: int = Query(...),
    conversation_id: str = Query(...),
    session: Session = Depends(get_db_session),
) -> ConversationRouteResponse:
    """Fetch a live conversation route (with its stored reference).

    Used by the outbound path to recover the ConversationReference and
    target assistant for a proactive reply.
    """
    dao = MsTeamsBotDAO(session)
    route = dao.get_conversation_route(install_id, conversation_id)
    if route is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No live conversation route found.",
        )
    return ConversationRouteResponse(
        id=route.id,
        install_id=route.install_id,
        conversation_id=route.conversation_id,
        assistant_id=route.assistant_id,
        conversation_reference=route.conversation_reference,
        expires_at=route.expires_at.isoformat() if route.expires_at else None,
    )


@admin_router.post("/ms-teams-bot/conversation-routes/prune")
def prune_expired_conversation_routes(
    session: Session = Depends(get_db_session),
):
    """Delete all expired conversation routes. Intended for a daily cron."""
    dao = MsTeamsBotDAO(session)
    count = dao.delete_expired_routes()
    session.commit()
    return {"deleted": count}
