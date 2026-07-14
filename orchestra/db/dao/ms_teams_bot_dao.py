"""Data access for MS Teams bot installs, channel bindings, conversation routes.

The Bot-Framework analogue of :mod:`orchestra.db.dao.slack_dao`. One DAO
surface over three tables that share a lifecycle (an install owns its
bindings and conversation routes) and that the dispatcher reads together
on every inbound activity.

The one structural departure from Slack is *deferred ownership*: a Teams
Store install gives us the Microsoft ``tenant_id`` before we know the
Unify owner, so an install is created **pending** (both owners NULL,
carrying a ``bind_nonce``) and later bound to an org/user via the
tenant-to-org handshake. The owner columns therefore allow the transient
both-NULL state; ``_require_at_most_one_owner`` forbids only the
both-set case.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    MsTeamsBotChannelBinding,
    MsTeamsBotConversationRoute,
    MsTeamsBotInstall,
)

logger = logging.getLogger(__name__)

DEFAULT_CONVERSATION_ROUTE_TTL_DAYS = 14


def _require_at_most_one_owner(
    organization_id: Optional[int],
    user_id: Optional[str],
) -> None:
    """Reject an install that claims *both* an org and a user owner.

    Unlike Slack's strict XOR, a Teams install may legitimately have
    *neither* owner set while it is pending the tenant-to-org bind. The DB
    CHECK (``ck_ms_teams_bot_install_single_owner``) enforces the same
    "not both" rule; rejecting in Python gives a clearer error on misuse.
    """
    if organization_id is not None and user_id is not None:
        raise ValueError(
            "MsTeamsBotInstall owner must be at most one of "
            "organization_id or user_id (got "
            f"organization_id={organization_id!r}, user_id={user_id!r}).",
        )


class MsTeamsBotDAO:
    """Reads and writes MS Teams bot install state for one orchestra session."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Installs
    # ------------------------------------------------------------------

    def get_install_by_tenant(
        self,
        tenant_id: str,
    ) -> Optional[MsTeamsBotInstall]:
        """Find the active install for a Microsoft tenant.

        Bot Framework activities carry the AAD ``tenant_id``; we resolve to
        the install row that owns the tenant and its owner scope (org or
        user). Revoked installs are ignored. May return a *pending* install
        (no owner yet) — callers routing traffic must treat that as
        "nothing to route to".
        """
        return (
            self.session.query(MsTeamsBotInstall)
            .filter(
                MsTeamsBotInstall.tenant_id == tenant_id,
                MsTeamsBotInstall.revoked_at.is_(None),
            )
            .first()
        )

    def get_install_by_nonce(
        self,
        bind_nonce: str,
    ) -> Optional[MsTeamsBotInstall]:
        """Find a pending install by its bind nonce (tenant-to-org handshake)."""
        return (
            self.session.query(MsTeamsBotInstall)
            .filter(
                MsTeamsBotInstall.bind_nonce == bind_nonce,
                MsTeamsBotInstall.revoked_at.is_(None),
            )
            .first()
        )

    def get_install_for_org(
        self,
        organization_id: int,
    ) -> Optional[MsTeamsBotInstall]:
        """Find the active install scoped to a Unify organization, if any."""
        return (
            self.session.query(MsTeamsBotInstall)
            .filter(
                MsTeamsBotInstall.organization_id == organization_id,
                MsTeamsBotInstall.revoked_at.is_(None),
            )
            .first()
        )

    def get_install_for_user(
        self,
        user_id: str,
    ) -> Optional[MsTeamsBotInstall]:
        """Find the active install scoped to a personal Unify user, if any."""
        return (
            self.session.query(MsTeamsBotInstall)
            .filter(
                MsTeamsBotInstall.user_id == user_id,
                MsTeamsBotInstall.organization_id.is_(None),
                MsTeamsBotInstall.revoked_at.is_(None),
            )
            .first()
        )

    def list_installs(self) -> list[MsTeamsBotInstall]:
        return (
            self.session.query(MsTeamsBotInstall).order_by(MsTeamsBotInstall.id).all()
        )

    def ensure_pending_install(
        self,
        *,
        tenant_id: str,
        bot_app_id: str,
        tenant_name: Optional[str] = None,
        service_url: Optional[str] = None,
        installer_aad_object_id: Optional[str] = None,
    ) -> tuple[MsTeamsBotInstall, bool]:
        """Create (or refresh) a *pending* install for a Microsoft tenant.

        Called on the first ``conversationUpdate`` (bot added to a tenant)
        before we know which Unify owner it belongs to. If an active
        install already exists for the tenant — pending or bound — its
        latest ``service_url`` / ``tenant_name`` / installer are refreshed
        and it is returned unchanged in ownership. A freshly created row
        carries a random ``bind_nonce`` used to complete the
        tenant-to-org handshake via :meth:`bind_install`.

        Returns ``(install, created)`` where ``created`` is ``True`` only
        when this call inserted a brand-new row. Callers use it to send the
        install welcome exactly once — a Teams add emits both an
        ``installationUpdate`` and a ``conversationUpdate`` (and may repeat),
        so welcoming on every event would spam the installer. The insert is
        race-safe: if a concurrent event wins the unique ``tenant_id`` slot
        first, the collision is caught and the now-existing row is returned
        with ``created=False``.
        """
        existing = self.get_install_by_tenant(tenant_id)
        if existing is not None:
            existing.bot_app_id = bot_app_id
            if tenant_name is not None:
                existing.tenant_name = tenant_name
            if service_url is not None:
                existing.service_url = service_url
            if installer_aad_object_id is not None:
                existing.installer_aad_object_id = installer_aad_object_id
            self.session.flush()
            return existing, False

        install = MsTeamsBotInstall(
            organization_id=None,
            user_id=None,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            bot_app_id=bot_app_id,
            service_url=service_url,
            installer_aad_object_id=installer_aad_object_id,
            bind_nonce=secrets.token_urlsafe(32),
        )
        self.session.add(install)
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError:
            # A concurrent add-event inserted the active row for this tenant
            # between our lookup and flush; fall back to the winner.
            self.session.expunge(install)
            existing = self.get_install_by_tenant(tenant_id)
            if existing is None:
                raise
            return existing, False
        return install, True

    def bind_install(
        self,
        install_id: int,
        *,
        organization_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> MsTeamsBotInstall:
        """Bind a pending install to a Unify owner (org XOR user).

        Completes the tenant-to-org handshake: consumes the ``bind_nonce``,
        stamps ``bound_at``, and sets exactly one owner. Rebinding a
        bound install to a new owner is allowed (e.g. correcting an
        install), which clears the other owner column.
        """
        if (organization_id is None) == (user_id is None):
            raise ValueError(
                "bind_install requires exactly one of organization_id or "
                f"user_id (got organization_id={organization_id!r}, "
                f"user_id={user_id!r}).",
            )
        install = (
            self.session.query(MsTeamsBotInstall)
            .filter(MsTeamsBotInstall.id == install_id)
            .first()
        )
        if install is None:
            raise ValueError(f"MsTeamsBotInstall {install_id} not found.")

        install.organization_id = organization_id
        install.user_id = user_id
        install.bind_nonce = None
        install.bound_at = datetime.now(timezone.utc)
        self.session.flush()
        return install

    def upsert_install(
        self,
        *,
        tenant_id: str,
        bot_app_id: str,
        organization_id: Optional[int] = None,
        user_id: Optional[str] = None,
        tenant_name: Optional[str] = None,
        service_url: Optional[str] = None,
        installer_aad_object_id: Optional[str] = None,
        scopes: Optional[str] = None,
    ) -> MsTeamsBotInstall:
        """Create or refresh an already-owned install for ``(owner, tenant)``.

        Used by the admin/Console path where the Unify owner is known up
        front (at most one of ``organization_id`` / ``user_id``). Refreshes
        the ``service_url`` and clears ``revoked_at`` so a re-install goes
        live again. For the Teams Store path where the owner is *not* yet
        known, use :meth:`ensure_pending_install` + :meth:`bind_install`.
        """
        _require_at_most_one_owner(organization_id, user_id)

        query = self.session.query(MsTeamsBotInstall).filter(
            MsTeamsBotInstall.tenant_id == tenant_id,
        )
        if organization_id is not None:
            query = query.filter(
                MsTeamsBotInstall.organization_id == organization_id,
            )
        elif user_id is not None:
            query = query.filter(MsTeamsBotInstall.user_id == user_id)
        existing = query.first()

        if existing is not None:
            existing.bot_app_id = bot_app_id
            existing.tenant_name = tenant_name
            if service_url is not None:
                existing.service_url = service_url
            if installer_aad_object_id is not None:
                existing.installer_aad_object_id = installer_aad_object_id
            existing.scopes = scopes
            existing.revoked_at = None
            if organization_id is not None or user_id is not None:
                existing.bind_nonce = None
                if existing.bound_at is None:
                    existing.bound_at = datetime.now(timezone.utc)
            self.session.flush()
            return existing

        install = MsTeamsBotInstall(
            organization_id=organization_id,
            user_id=user_id,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            bot_app_id=bot_app_id,
            service_url=service_url,
            installer_aad_object_id=installer_aad_object_id,
            scopes=scopes,
            bound_at=(
                datetime.now(timezone.utc)
                if (organization_id is not None or user_id is not None)
                else None
            ),
        )
        self.session.add(install)
        self.session.flush()
        return install

    def update_service_url(
        self,
        install_id: int,
        service_url: str,
    ) -> Optional[MsTeamsBotInstall]:
        """Refresh the tenant's Bot Framework ``service_url``.

        The service url is region-specific and can change; it is echoed on
        every inbound activity, so we opportunistically keep the freshest
        value for outbound proactive replies.
        """
        install = (
            self.session.query(MsTeamsBotInstall)
            .filter(MsTeamsBotInstall.id == install_id)
            .first()
        )
        if install is None:
            return None
        if service_url and install.service_url != service_url:
            install.service_url = service_url
            self.session.flush()
        return install

    def revoke_install(
        self,
        install_id: int,
    ) -> Optional[MsTeamsBotInstall]:
        """Mark an install revoked; drop its bindings and conversation routes."""
        install = (
            self.session.query(MsTeamsBotInstall)
            .filter(MsTeamsBotInstall.id == install_id)
            .first()
        )
        if install is None:
            return None
        install.revoked_at = datetime.now(timezone.utc)
        self.session.query(MsTeamsBotChannelBinding).filter(
            MsTeamsBotChannelBinding.install_id == install_id,
        ).delete(synchronize_session=False)
        self.session.query(MsTeamsBotConversationRoute).filter(
            MsTeamsBotConversationRoute.install_id == install_id,
        ).delete(synchronize_session=False)
        self.session.flush()
        return install

    # ------------------------------------------------------------------
    # Channel bindings
    # ------------------------------------------------------------------

    def bind_channel(
        self,
        install_id: int,
        channel_id: str,
        assistant_id: int,
        channel_name: Optional[str] = None,
    ) -> MsTeamsBotChannelBinding:
        """Set or update the default assistant for a Teams channel."""
        existing = (
            self.session.query(MsTeamsBotChannelBinding)
            .filter(
                MsTeamsBotChannelBinding.install_id == install_id,
                MsTeamsBotChannelBinding.channel_id == channel_id,
            )
            .first()
        )
        if existing is not None:
            existing.assistant_id = assistant_id
            if channel_name is not None:
                existing.channel_name = channel_name
            self.session.flush()
            return existing

        binding = MsTeamsBotChannelBinding(
            install_id=install_id,
            channel_id=channel_id,
            channel_name=channel_name,
            assistant_id=assistant_id,
        )
        self.session.add(binding)
        self.session.flush()
        return binding

    def unbind_channel(self, install_id: int, channel_id: str) -> bool:
        result = (
            self.session.query(MsTeamsBotChannelBinding)
            .filter(
                MsTeamsBotChannelBinding.install_id == install_id,
                MsTeamsBotChannelBinding.channel_id == channel_id,
            )
            .delete()
        )
        self.session.flush()
        return bool(result)

    def get_channel_binding(
        self,
        install_id: int,
        channel_id: str,
    ) -> Optional[MsTeamsBotChannelBinding]:
        return (
            self.session.query(MsTeamsBotChannelBinding)
            .filter(
                MsTeamsBotChannelBinding.install_id == install_id,
                MsTeamsBotChannelBinding.channel_id == channel_id,
            )
            .first()
        )

    def list_channel_bindings(
        self,
        install_id: int,
    ) -> list[MsTeamsBotChannelBinding]:
        return (
            self.session.query(MsTeamsBotChannelBinding)
            .filter(MsTeamsBotChannelBinding.install_id == install_id)
            .order_by(MsTeamsBotChannelBinding.id)
            .all()
        )

    # ------------------------------------------------------------------
    # Conversation routes (1:1 chats, group chats, channel threads)
    # ------------------------------------------------------------------

    def get_conversation_route(
        self,
        install_id: int,
        conversation_id: str,
    ) -> Optional[MsTeamsBotConversationRoute]:
        """Return the live (non-expired) route for a conversation."""
        now = datetime.now(timezone.utc)
        return (
            self.session.query(MsTeamsBotConversationRoute)
            .filter(
                MsTeamsBotConversationRoute.install_id == install_id,
                MsTeamsBotConversationRoute.conversation_id == conversation_id,
                MsTeamsBotConversationRoute.expires_at > now,
            )
            .first()
        )

    def upsert_conversation_route(
        self,
        install_id: int,
        conversation_id: str,
        assistant_id: int,
        conversation_reference: Optional[str] = None,
        ttl_days: int = DEFAULT_CONVERSATION_ROUTE_TTL_DAYS,
    ) -> MsTeamsBotConversationRoute:
        """Pin a conversation to an assistant and refresh its TTL.

        Idempotent. Subsequent calls with the same ``conversation_id``
        update ``assistant_id`` (allowing in-conversation hand-off), refresh
        the stored ``conversation_reference`` (so outbound always has the
        freshest proactive-reply target), and reset ``last_used_at`` /
        ``expires_at``.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)
        existing = (
            self.session.query(MsTeamsBotConversationRoute)
            .filter(
                MsTeamsBotConversationRoute.install_id == install_id,
                MsTeamsBotConversationRoute.conversation_id == conversation_id,
            )
            .first()
        )
        if existing is not None:
            existing.assistant_id = assistant_id
            if conversation_reference is not None:
                existing.conversation_reference = conversation_reference
            existing.last_used_at = now
            existing.expires_at = expires_at
            self.session.flush()
            return existing

        route = MsTeamsBotConversationRoute(
            install_id=install_id,
            conversation_id=conversation_id,
            assistant_id=assistant_id,
            conversation_reference=conversation_reference,
            last_used_at=now,
            expires_at=expires_at,
        )
        self.session.add(route)
        self.session.flush()
        return route

    def touch_conversation_route(
        self,
        route: MsTeamsBotConversationRoute,
        ttl_days: int = DEFAULT_CONVERSATION_ROUTE_TTL_DAYS,
    ) -> MsTeamsBotConversationRoute:
        """Refresh ``last_used_at`` / ``expires_at`` on a route we just hit."""
        now = datetime.now(timezone.utc)
        route.last_used_at = now
        route.expires_at = now + timedelta(days=ttl_days)
        self.session.flush()
        return route

    def delete_expired_routes(self, before: Optional[datetime] = None) -> int:
        """Bulk-prune routes that have expired. Returns rows deleted."""
        cutoff = before or datetime.now(timezone.utc)
        result = self.session.execute(
            delete(MsTeamsBotConversationRoute).where(
                MsTeamsBotConversationRoute.expires_at <= cutoff,
            ),
        )
        self.session.flush()
        return int(result.rowcount or 0)
