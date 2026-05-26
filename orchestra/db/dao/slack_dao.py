"""Data access for Slack installs, channel bindings, and thread routes.

One DAO surface, three tables — they share a lifecycle (an install owns
its bindings and thread routes) and the dispatcher needs to read from all
three on every inbound event. Splitting them across files would force the
dispatcher to instantiate three DAOs per call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    DM_ROOT_SENTINEL,
    SlackChannelBinding,
    SlackInstall,
    SlackThreadRoute,
)

logger = logging.getLogger(__name__)

DEFAULT_THREAD_ROUTE_TTL_DAYS = 14


class SlackDAO:
    """Reads and writes Slack install state for one orchestra session."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Installs
    # ------------------------------------------------------------------

    def get_install_by_team(self, slack_team_id: str) -> Optional[SlackInstall]:
        """Find the active install for a Slack workspace.

        Slack events carry ``team_id``; we resolve to the install row that
        owns the bot token and organization scope. Revoked installs are
        ignored.
        """
        return (
            self.session.query(SlackInstall)
            .filter(
                SlackInstall.slack_team_id == slack_team_id,
                SlackInstall.revoked_at.is_(None),
            )
            .first()
        )

    def get_install_for_org(self, organization_id: int) -> Optional[SlackInstall]:
        """Find the active install scoped to a Unify organization, if any."""
        return (
            self.session.query(SlackInstall)
            .filter(
                SlackInstall.organization_id == organization_id,
                SlackInstall.revoked_at.is_(None),
            )
            .first()
        )

    def list_installs(self) -> list[SlackInstall]:
        return self.session.query(SlackInstall).order_by(SlackInstall.id).all()

    def upsert_install(
        self,
        *,
        organization_id: int,
        slack_team_id: str,
        slack_app_id: str,
        bot_user_id: str,
        bot_access_token: str,
        slack_team_name: Optional[str] = None,
        enterprise_id: Optional[str] = None,
        installer_user_id: Optional[str] = None,
        scopes: Optional[str] = None,
    ) -> SlackInstall:
        """Create or refresh an install for ``(org, team)``.

        Re-installs replace the bot token (Slack rotates tokens on
        re-auth) and clear ``revoked_at`` so the install is live again.
        """
        existing = (
            self.session.query(SlackInstall)
            .filter(
                SlackInstall.organization_id == organization_id,
                SlackInstall.slack_team_id == slack_team_id,
            )
            .first()
        )
        if existing is not None:
            existing.slack_app_id = slack_app_id
            existing.bot_user_id = bot_user_id
            existing.bot_access_token = bot_access_token
            existing.slack_team_name = slack_team_name
            existing.enterprise_id = enterprise_id
            existing.installer_user_id = installer_user_id
            existing.scopes = scopes
            existing.revoked_at = None
            self.session.flush()
            return existing

        install = SlackInstall(
            organization_id=organization_id,
            slack_team_id=slack_team_id,
            slack_team_name=slack_team_name,
            slack_app_id=slack_app_id,
            enterprise_id=enterprise_id,
            bot_user_id=bot_user_id,
            bot_access_token=bot_access_token,
            installer_user_id=installer_user_id,
            scopes=scopes,
        )
        self.session.add(install)
        self.session.flush()
        return install

    def revoke_install(self, install_id: int) -> Optional[SlackInstall]:
        """Mark an install as revoked. Bindings and thread routes cascade-delete."""
        install = (
            self.session.query(SlackInstall)
            .filter(SlackInstall.id == install_id)
            .first()
        )
        if install is None:
            return None
        install.revoked_at = datetime.now(timezone.utc)
        # Cascade is set on the FKs; deleting the install would remove
        # bindings/routes. We keep the row (audit trail) and instead
        # null out the routing state explicitly.
        self.session.query(SlackChannelBinding).filter(
            SlackChannelBinding.install_id == install_id,
        ).delete(synchronize_session=False)
        self.session.query(SlackThreadRoute).filter(
            SlackThreadRoute.install_id == install_id,
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
    ) -> SlackChannelBinding:
        """Set or update the default assistant for a channel."""
        existing = (
            self.session.query(SlackChannelBinding)
            .filter(
                SlackChannelBinding.install_id == install_id,
                SlackChannelBinding.channel_id == channel_id,
            )
            .first()
        )
        if existing is not None:
            existing.assistant_id = assistant_id
            if channel_name is not None:
                existing.channel_name = channel_name
            self.session.flush()
            return existing

        binding = SlackChannelBinding(
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
            self.session.query(SlackChannelBinding)
            .filter(
                SlackChannelBinding.install_id == install_id,
                SlackChannelBinding.channel_id == channel_id,
            )
            .delete()
        )
        self.session.flush()
        return bool(result)

    def get_channel_binding(
        self,
        install_id: int,
        channel_id: str,
    ) -> Optional[SlackChannelBinding]:
        return (
            self.session.query(SlackChannelBinding)
            .filter(
                SlackChannelBinding.install_id == install_id,
                SlackChannelBinding.channel_id == channel_id,
            )
            .first()
        )

    def list_channel_bindings(self, install_id: int) -> list[SlackChannelBinding]:
        return (
            self.session.query(SlackChannelBinding)
            .filter(SlackChannelBinding.install_id == install_id)
            .order_by(SlackChannelBinding.id)
            .all()
        )

    # ------------------------------------------------------------------
    # Thread routes (channel threads + DM roots)
    # ------------------------------------------------------------------

    def get_thread_route(
        self,
        install_id: int,
        channel_id: str,
        thread_ts: str,
    ) -> Optional[SlackThreadRoute]:
        """Return the live (non-expired) route for a conversation."""
        now = datetime.now(timezone.utc)
        return (
            self.session.query(SlackThreadRoute)
            .filter(
                SlackThreadRoute.install_id == install_id,
                SlackThreadRoute.channel_id == channel_id,
                SlackThreadRoute.thread_ts == thread_ts,
                SlackThreadRoute.expires_at > now,
            )
            .first()
        )

    def get_dm_route(
        self,
        install_id: int,
        dm_channel_id: str,
    ) -> Optional[SlackThreadRoute]:
        """Return the live route for a 1:1 DM with the bot."""
        return self.get_thread_route(install_id, dm_channel_id, DM_ROOT_SENTINEL)

    def upsert_thread_route(
        self,
        install_id: int,
        channel_id: str,
        thread_ts: str,
        assistant_id: int,
        ttl_days: int = DEFAULT_THREAD_ROUTE_TTL_DAYS,
    ) -> SlackThreadRoute:
        """Pin a conversation to an assistant and refresh its TTL.

        Idempotent. Subsequent calls with the same conversation key
        update ``assistant_id`` (allowing in-thread hand-off) and reset
        ``last_used_at`` / ``expires_at``.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)
        existing = (
            self.session.query(SlackThreadRoute)
            .filter(
                SlackThreadRoute.install_id == install_id,
                SlackThreadRoute.channel_id == channel_id,
                SlackThreadRoute.thread_ts == thread_ts,
            )
            .first()
        )
        if existing is not None:
            existing.assistant_id = assistant_id
            existing.last_used_at = now
            existing.expires_at = expires_at
            self.session.flush()
            return existing

        route = SlackThreadRoute(
            install_id=install_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            assistant_id=assistant_id,
            last_used_at=now,
            expires_at=expires_at,
        )
        self.session.add(route)
        self.session.flush()
        return route

    def upsert_dm_route(
        self,
        install_id: int,
        dm_channel_id: str,
        assistant_id: int,
        ttl_days: int = DEFAULT_THREAD_ROUTE_TTL_DAYS,
    ) -> SlackThreadRoute:
        return self.upsert_thread_route(
            install_id,
            dm_channel_id,
            DM_ROOT_SENTINEL,
            assistant_id,
            ttl_days=ttl_days,
        )

    def touch_thread_route(
        self,
        route: SlackThreadRoute,
        ttl_days: int = DEFAULT_THREAD_ROUTE_TTL_DAYS,
    ) -> SlackThreadRoute:
        """Refresh ``last_used_at`` / ``expires_at`` on a route we just hit.

        Called when an inbound or outbound message lands on an existing
        route — sliding TTL prevents active conversations from expiring.
        """
        now = datetime.now(timezone.utc)
        route.last_used_at = now
        route.expires_at = now + timedelta(days=ttl_days)
        self.session.flush()
        return route

    def delete_expired_routes(self, before: Optional[datetime] = None) -> int:
        """Bulk-prune routes that have expired. Returns rows deleted."""
        cutoff = before or datetime.now(timezone.utc)
        result = self.session.execute(
            delete(SlackThreadRoute).where(SlackThreadRoute.expires_at <= cutoff),
        )
        self.session.flush()
        return int(result.rowcount or 0)
