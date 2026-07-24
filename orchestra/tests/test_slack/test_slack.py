"""Tests for the Slack integration: schema, DAO, dispatcher, admin endpoints.

Covers:

* Install/binding/route schema constraints and lifecycle (DAO), in both
  organizational and personal (user-owned) install modes.
* The polymorphic ownership invariants on ``SlackInstall``: XOR between
  ``organization_id`` and ``user_id`` and the active-team uniqueness
  that prevents two owners from holding the same Slack workspace live.
* The full dispatcher routing tree — DM vs channel, token addressing
  (unique / unknown / ambiguous), thread inheritance, channel binding
  fallback, bot-echo suppression — for both owner kinds.
* ``AssistantDAO`` additions (``coordinator``, ``resolve_token``) scoped
  by either organization or personal user.
* The admin HTTP surface end-to-end through the FastAPI client.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.models.orchestra_models import (
    DM_ROOT_SENTINEL,
    Assistant,
    BillingAccount,
    Context,
    LogEvent,
    LogEventContext,
    Organization,
    Project,
    SlackChannelBinding,
    SlackInstall,
    SlackThreadRoute,
    User,
)
from orchestra.services.assistant_bootstrap import (
    _assistant_context_name,
    _resolve_assistants_project,
    ensure_owner_contact_row,
)
from orchestra.services.contact_membership_service import PERSONAL_BOSS_CONTACT_ID
from orchestra.services.slack_dispatcher import resolve_inbound
from orchestra.tests.utils import ADMIN_HEADERS

# ============================================================================
# Fixtures / helpers
# ============================================================================


def _make_user(dbsession: Session, suffix: str) -> User:
    ba = BillingAccount(credits=100)
    dbsession.add(ba)
    dbsession.flush()
    user = User(
        id=f"slack-user-{suffix}-{uuid.uuid4().hex[:8]}",
        email=f"slack-user-{suffix}-{uuid.uuid4().hex[:8]}@test.com",
        name=f"User {suffix}",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_org(dbsession: Session, owner: User, suffix: str) -> Organization:
    org = Organization(owner_id=owner.id, name=f"Slack Org {suffix}")
    dbsession.add(org)
    dbsession.flush()
    return org


def _make_assistant(
    dbsession: Session,
    owner: User,
    *,
    first_name: str,
    organization: Optional[Organization] = None,
    is_coordinator: bool = False,
) -> Assistant:
    """Create an assistant.

    Pass ``organization=None`` for a personal assistant
    (``organization_id IS NULL``); pass an org for an organizational one.
    """
    assistant = Assistant(
        user_id=owner.id,
        organization_id=organization.id if organization is not None else None,
        first_name=first_name,
        surname="Bot",
        is_coordinator=is_coordinator,
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_install(
    dbsession: Session,
    *,
    organization: Optional[Organization] = None,
    user: Optional[User] = None,
    slack_team_id: str = "T01TEAM",
    bot_user_id: str = "U01BOT",
    bot_access_token: str = "xoxb-test",
) -> SlackInstall:
    """Create an install owned by either an org or a personal user."""
    if (organization is None) == (user is None):
        raise ValueError("Provide exactly one of organization or user.")
    install = SlackInstall(
        organization_id=organization.id if organization is not None else None,
        user_id=user.id if user is not None else None,
        slack_team_id=slack_team_id,
        slack_team_name="Test Workspace",
        slack_app_id="A01APP",
        bot_user_id=bot_user_id,
        bot_access_token=bot_access_token,
        installer_user_id="U01INSTALLER",
        scopes="chat:write,im:history,channels:history",
    )
    dbsession.add(install)
    dbsession.flush()
    return install


def _make_assistants_project(
    dbsession: Session,
    *,
    organization: Optional[Organization] = None,
    user: Optional[User] = None,
) -> Project:
    if (organization is None) == (user is None):
        raise ValueError("Provide exactly one of organization or user.")
    project = Project(
        name="Assistants",
        organization_id=organization.id if organization is not None else None,
        user_id=user.id if user is not None else None,
    )
    dbsession.add(project)
    dbsession.flush()
    return project


def _boss_contact_data(
    dbsession: Session,
    *,
    assistant: Assistant,
) -> dict | None:
    project = _resolve_assistants_project(dbsession, assistant=assistant)
    context = dbsession.scalar(
        select(Context).where(
            Context.project_id == project.id,
            Context.name == _assistant_context_name(assistant, "Contacts"),
        ),
    )
    if context is None:
        return None
    logs = dbsession.scalars(
        select(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(LogEventContext.context_id == context.id),
    ).all()
    for log in logs:
        if log.data.get("contact_id") == PERSONAL_BOSS_CONTACT_ID:
            return log.data
    return None


@pytest.fixture
def slack_world(dbsession: Session):
    """Minimal world: org with a Coordinator + named assistants + Slack install."""
    owner = _make_user(dbsession, "owner")
    org = _make_org(dbsession, owner, "slack")
    coordinator = _make_assistant(
        dbsession,
        owner,
        first_name="T-W1N",
        organization=org,
        is_coordinator=True,
    )
    alex = _make_assistant(
        dbsession,
        owner,
        first_name="Alex",
        organization=org,
    )
    sara = _make_assistant(
        dbsession,
        owner,
        first_name="Sara",
        organization=org,
    )
    install = _make_install(dbsession, organization=org)
    return {
        "owner": owner,
        "org": org,
        "coordinator": coordinator,
        "alex": alex,
        "sara": sara,
        "install": install,
    }


@pytest.fixture
def personal_slack_world(dbsession: Session):
    """Personal-mode world: one user with personal coordinator + assistants + install.

    Mirrors :func:`slack_world` but with ``organization_id IS NULL`` on
    the assistants and a personal-user-owned ``SlackInstall``. The Slack
    workspace id is deliberately distinct so personal and org worlds
    coexist in the same test DB without colliding on
    ``ux_slack_install_active_team``.
    """
    user = _make_user(dbsession, "personal-owner")
    coordinator = _make_assistant(
        dbsession,
        user,
        first_name="T-W1N",
        organization=None,
        is_coordinator=True,
    )
    alex = _make_assistant(dbsession, user, first_name="Alex", organization=None)
    sara = _make_assistant(dbsession, user, first_name="Sara", organization=None)
    install = _make_install(
        dbsession,
        user=user,
        slack_team_id="T02PERSONAL",
        bot_user_id="U02BOT",
    )
    return {
        "user": user,
        "coordinator": coordinator,
        "alex": alex,
        "sara": sara,
        "install": install,
    }


# ============================================================================
# Schema invariants
# ============================================================================


class TestSchema:
    """Direct schema-level invariants — unique constraints, cascade behavior."""

    def test_install_unique_per_org_team_against_revoked_row(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """The per-owner partial unique catches duplicates even after revoke.

        Revoking the prior install frees up the active-team uniqueness
        (``ux_slack_install_active_team``) but the per-owner uniqueness
        (``ux_slack_install_org_team``) still prevents the same owner
        from owning two rows for the same workspace.
        """
        install = slack_world["install"]
        org = slack_world["org"]
        install.revoked_at = datetime.now(timezone.utc)
        dbsession.flush()

        duplicate = SlackInstall(
            organization_id=org.id,
            slack_team_id="T01TEAM",
            slack_app_id="A01APP",
            bot_user_id="U01BOT2",
            bot_access_token="xoxb-dup",
        )
        dbsession.add(duplicate)
        with pytest.raises(IntegrityError, match="ux_slack_install_org_team"):
            dbsession.flush()

    def test_install_unique_per_user_team_against_revoked_row(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        """Same per-owner uniqueness invariant, personal-mode variant."""
        install = personal_slack_world["install"]
        user = personal_slack_world["user"]
        install.revoked_at = datetime.now(timezone.utc)
        dbsession.flush()

        duplicate = SlackInstall(
            user_id=user.id,
            slack_team_id="T02PERSONAL",
            slack_app_id="A02APP",
            bot_user_id="U02BOT2",
            bot_access_token="xoxb-dup",
        )
        dbsession.add(duplicate)
        with pytest.raises(IntegrityError, match="ux_slack_install_user_team"):
            dbsession.flush()

    def test_install_active_duplicate_for_same_owner_is_rejected(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Two active installs for the same (owner, workspace) is impossible.

        Both the per-owner and the active-team partial uniques apply; we
        only care that *some* uniqueness violation fires, not which one
        the DB happens to catch first.
        """
        org = slack_world["org"]
        duplicate = SlackInstall(
            organization_id=org.id,
            slack_team_id="T01TEAM",
            slack_app_id="A01APP",
            bot_user_id="U01BOT2",
            bot_access_token="xoxb-dup",
        )
        dbsession.add(duplicate)
        with pytest.raises(IntegrityError):
            dbsession.flush()

    def test_install_rejects_both_owners(self, dbsession: Session) -> None:
        """A row with both ``organization_id`` and ``user_id`` set must be rejected."""
        user = _make_user(dbsession, "both-owners")
        org = _make_org(dbsession, user, "both-owners")
        bad = SlackInstall(
            organization_id=org.id,
            user_id=user.id,
            slack_team_id="T_BOTH",
            slack_app_id="A_BOTH",
            bot_user_id="U_BOTH",
            bot_access_token="xoxb-bad",
        )
        dbsession.add(bad)
        with pytest.raises(IntegrityError, match="ck_slack_install_one_owner"):
            dbsession.flush()

    def test_install_rejects_no_owner(self, dbsession: Session) -> None:
        """A row with neither ``organization_id`` nor ``user_id`` must be rejected."""
        bad = SlackInstall(
            organization_id=None,
            user_id=None,
            slack_team_id="T_NONE",
            slack_app_id="A_NONE",
            bot_user_id="U_NONE",
            bot_access_token="xoxb-bad",
        )
        dbsession.add(bad)
        with pytest.raises(IntegrityError, match="ck_slack_install_one_owner"):
            dbsession.flush()

    def test_active_team_uniqueness_across_owners(
        self,
        dbsession: Session,
    ) -> None:
        """Two distinct owners cannot both hold the same workspace live.

        A Slack workspace has a single bot identity at a time, so the
        ``ux_slack_install_active_team`` partial unique index rejects a
        second active install for the same ``slack_team_id`` regardless
        of who tries to claim it.
        """
        user_a = _make_user(dbsession, "active-a")
        org = _make_org(dbsession, user_a, "active-a")
        _make_install(
            dbsession,
            organization=org,
            slack_team_id="T_SHARED",
            bot_user_id="U_A",
            bot_access_token="xoxb-a",
        )
        user_b = _make_user(dbsession, "active-b")
        conflict = SlackInstall(
            user_id=user_b.id,
            slack_team_id="T_SHARED",
            slack_app_id="A_B",
            bot_user_id="U_B",
            bot_access_token="xoxb-b",
        )
        dbsession.add(conflict)
        with pytest.raises(IntegrityError, match="ux_slack_install_active_team"):
            dbsession.flush()

    def test_active_team_uniqueness_allows_reinstall_after_revoke(
        self,
        dbsession: Session,
    ) -> None:
        """A different owner can install the workspace after the prior install is revoked."""
        user_a = _make_user(dbsession, "after-revoke-a")
        org = _make_org(dbsession, user_a, "after-revoke-a")
        first = _make_install(
            dbsession,
            organization=org,
            slack_team_id="T_HANDOFF",
            bot_user_id="U_A",
        )
        first.revoked_at = datetime.now(timezone.utc)
        dbsession.flush()

        user_b = _make_user(dbsession, "after-revoke-b")
        second = _make_install(
            dbsession,
            user=user_b,
            slack_team_id="T_HANDOFF",
            bot_user_id="U_B",
        )
        assert second.id != first.id

    def test_channel_binding_unique_per_install_channel(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        install = slack_world["install"]
        alex = slack_world["alex"]
        sara = slack_world["sara"]
        dbsession.add(
            SlackChannelBinding(
                install_id=install.id,
                channel_id="C01TEAM",
                assistant_id=alex.agent_id,
            ),
        )
        dbsession.flush()

        dbsession.add(
            SlackChannelBinding(
                install_id=install.id,
                channel_id="C01TEAM",
                assistant_id=sara.agent_id,
            ),
        )
        with pytest.raises(IntegrityError, match="uq_slack_channel_binding"):
            dbsession.flush()

    def test_thread_route_unique_per_conversation(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        install = slack_world["install"]
        alex = slack_world["alex"]
        sara = slack_world["sara"]
        expires_at = datetime.now(timezone.utc) + timedelta(days=14)
        dbsession.add(
            SlackThreadRoute(
                install_id=install.id,
                channel_id="C01TEAM",
                thread_ts="1700000000.000100",
                assistant_id=alex.agent_id,
                expires_at=expires_at,
            ),
        )
        dbsession.flush()

        dbsession.add(
            SlackThreadRoute(
                install_id=install.id,
                channel_id="C01TEAM",
                thread_ts="1700000000.000100",
                assistant_id=sara.agent_id,
                expires_at=expires_at,
            ),
        )
        with pytest.raises(IntegrityError, match="uq_slack_thread_route"):
            dbsession.flush()

    def test_install_hard_delete_cascades_to_bindings_and_routes(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``ondelete=CASCADE`` on bindings + routes must fire on real deletes.

        ``revoke_install`` is a soft revoke that hand-removes children; the
        cascade is the safety net for any code path that hard-deletes
        (e.g. fixture teardown, org wipe, manual cleanup).
        """
        install = slack_world["install"]
        alex = slack_world["alex"]
        SlackDAO(dbsession).bind_channel(install.id, "C_CASCADE", alex.agent_id)
        SlackDAO(dbsession).upsert_thread_route(
            install.id,
            "C_CASCADE",
            "1700000010.0001",
            alex.agent_id,
        )

        install_id = install.id
        dbsession.delete(install)
        dbsession.flush()

        assert (
            dbsession.query(SlackChannelBinding)
            .filter(SlackChannelBinding.install_id == install_id)
            .count()
            == 0
        )
        assert (
            dbsession.query(SlackThreadRoute)
            .filter(SlackThreadRoute.install_id == install_id)
            .count()
            == 0
        )


# ============================================================================
# SlackDAO
# ============================================================================


class TestSlackDAO:
    def test_upsert_install_creates_then_refreshes(
        self,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "upsert")
        org = _make_org(dbsession, owner, "upsert")
        dao = SlackDAO(dbsession)

        created = dao.upsert_install(
            organization_id=org.id,
            slack_team_id="T_UPSERT",
            slack_app_id="A_UPSERT",
            bot_user_id="U_UPSERT",
            bot_access_token="xoxb-old",
        )
        assert created.id is not None
        assert created.bot_access_token == "xoxb-old"

        refreshed = dao.upsert_install(
            organization_id=org.id,
            slack_team_id="T_UPSERT",
            slack_app_id="A_UPSERT",
            bot_user_id="U_UPSERT",
            bot_access_token="xoxb-new",
            scopes="im:history,chat:write",
        )
        assert refreshed.id == created.id
        assert refreshed.bot_access_token == "xoxb-new"
        assert refreshed.scopes == "im:history,chat:write"
        assert refreshed.revoked_at is None

    def test_get_install_by_team_ignores_revoked(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]

        assert dao.get_install_by_team("T01TEAM").id == install.id

        dao.revoke_install(install.id)
        assert dao.get_install_by_team("T01TEAM") is None

    def test_revoke_install_drops_bindings_and_routes(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]

        dao.bind_channel(install.id, "C_TEAM", alex.agent_id)
        dao.upsert_thread_route(install.id, "C_TEAM", "1700000000.0001", alex.agent_id)

        dao.revoke_install(install.id)

        assert (
            dbsession.query(SlackChannelBinding)
            .filter(SlackChannelBinding.install_id == install.id)
            .count()
            == 0
        )
        assert (
            dbsession.query(SlackThreadRoute)
            .filter(SlackThreadRoute.install_id == install.id)
            .count()
            == 0
        )

    def test_bind_channel_is_idempotent_and_reassigns(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]
        sara = slack_world["sara"]

        first = dao.bind_channel(
            install.id,
            "C_GENERAL",
            alex.agent_id,
            channel_name="general",
        )
        second = dao.bind_channel(install.id, "C_GENERAL", sara.agent_id)
        assert first.id == second.id
        assert second.assistant_id == sara.agent_id
        assert second.channel_name == "general"

    def test_thread_route_upsert_refreshes_ttl(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]

        route = dao.upsert_thread_route(
            install.id,
            "C_GENERAL",
            "1700000000.000100",
            alex.agent_id,
        )
        original_expiry = route.expires_at

        # Backdate the route so the refresh produces a strictly later
        # expiry without depending on wall-clock granularity.
        route.expires_at = original_expiry - timedelta(minutes=5)
        dbsession.flush()

        refreshed = dao.upsert_thread_route(
            install.id,
            "C_GENERAL",
            "1700000000.000100",
            alex.agent_id,
        )
        assert refreshed.id == route.id
        assert refreshed.expires_at > original_expiry - timedelta(minutes=5)

    def test_get_thread_route_filters_expired(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]

        route = dao.upsert_thread_route(
            install.id,
            "C_GENERAL",
            "1700000000.000200",
            alex.agent_id,
        )
        route.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        dbsession.flush()

        assert (
            dao.get_thread_route(install.id, "C_GENERAL", "1700000000.000200") is None
        )

    def test_delete_expired_routes_prunes(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]

        live = dao.upsert_thread_route(
            install.id,
            "C_LIVE",
            "1700000000.000300",
            alex.agent_id,
        )
        expired = dao.upsert_thread_route(
            install.id,
            "C_EXPIRED",
            "1700000000.000400",
            alex.agent_id,
        )
        expired.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        dbsession.flush()

        deleted = dao.delete_expired_routes()
        assert deleted == 1
        remaining = dbsession.query(SlackThreadRoute).all()
        assert {r.id for r in remaining} == {live.id}

    def test_dm_helpers_use_sentinel(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]

        dao.upsert_dm_route(install.id, "D01DM", alex.agent_id)

        route = dao.get_dm_route(install.id, "D01DM")
        assert route is not None
        assert route.thread_ts == DM_ROOT_SENTINEL
        assert route.assistant_id == alex.agent_id

    def test_reinstall_after_revoke_clears_revoked_at(
        self,
        dbsession: Session,
    ) -> None:
        """A re-OAuth on the same workspace should bring a revoked install back to life."""
        owner = _make_user(dbsession, "reinstall")
        org = _make_org(dbsession, owner, "reinstall")
        dao = SlackDAO(dbsession)
        install = dao.upsert_install(
            organization_id=org.id,
            slack_team_id="T_RE",
            slack_app_id="A_RE",
            bot_user_id="U_RE",
            bot_access_token="xoxb-1",
        )
        dao.revoke_install(install.id)
        assert install.revoked_at is not None

        refreshed = dao.upsert_install(
            organization_id=org.id,
            slack_team_id="T_RE",
            slack_app_id="A_RE",
            bot_user_id="U_RE_NEW",
            bot_access_token="xoxb-2",
        )
        assert refreshed.id == install.id
        assert refreshed.revoked_at is None
        assert refreshed.bot_user_id == "U_RE_NEW"
        assert refreshed.bot_access_token == "xoxb-2"

    def test_get_install_for_org_ignores_revoked(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        org_id = slack_world["org"].id
        assert dao.get_install_for_org(org_id) is not None
        dao.revoke_install(slack_world["install"].id)
        assert dao.get_install_for_org(org_id) is None

    def test_get_install_for_user_ignores_revoked(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        user_id = personal_slack_world["user"].id
        assert dao.get_install_for_user(user_id) is not None
        dao.revoke_install(personal_slack_world["install"].id)
        assert dao.get_install_for_user(user_id) is None

    def test_get_install_for_user_does_not_match_org_installs(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``get_install_for_user`` must scope to personal-owned installs only.

        The owner of the org-mode install also has a ``user_id`` on the
        org row's owner, but that user has *no* personal install — the
        lookup must return ``None``.
        """
        dao = SlackDAO(dbsession)
        owner_user_id = slack_world["owner"].id
        assert dao.get_install_for_user(owner_user_id) is None

    def test_upsert_install_personal_creates_then_refreshes(
        self,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "upsert-personal")
        dao = SlackDAO(dbsession)
        created = dao.upsert_install(
            user_id=user.id,
            slack_team_id="T_UPSERT_PERSONAL",
            slack_app_id="A_UPSERT",
            bot_user_id="U_UPSERT",
            bot_access_token="xoxb-old",
        )
        assert created.id is not None
        assert created.user_id == user.id
        assert created.organization_id is None

        refreshed = dao.upsert_install(
            user_id=user.id,
            slack_team_id="T_UPSERT_PERSONAL",
            slack_app_id="A_UPSERT",
            bot_user_id="U_UPSERT",
            bot_access_token="xoxb-new",
        )
        assert refreshed.id == created.id
        assert refreshed.bot_access_token == "xoxb-new"

    def test_upsert_install_requires_owner_xor(
        self,
        dbsession: Session,
    ) -> None:
        dao = SlackDAO(dbsession)
        with pytest.raises(ValueError):
            dao.upsert_install(
                slack_team_id="T_X",
                slack_app_id="A_X",
                bot_user_id="U_X",
                bot_access_token="xoxb-x",
            )
        with pytest.raises(ValueError):
            dao.upsert_install(
                organization_id=1,
                user_id="some-user",
                slack_team_id="T_X",
                slack_app_id="A_X",
                bot_user_id="U_X",
                bot_access_token="xoxb-x",
            )

    def test_upsert_install_org_and_user_keep_distinct_rows(
        self,
        dbsession: Session,
    ) -> None:
        """Distinct owners on distinct workspaces yield distinct installs."""
        user = _make_user(dbsession, "distinct-owners")
        org = _make_org(dbsession, user, "distinct-owners")
        dao = SlackDAO(dbsession)
        org_install = dao.upsert_install(
            organization_id=org.id,
            slack_team_id="T_ORG",
            slack_app_id="A_ORG",
            bot_user_id="U_ORG",
            bot_access_token="xoxb-org",
        )
        user_install = dao.upsert_install(
            user_id=user.id,
            slack_team_id="T_USER",
            slack_app_id="A_USER",
            bot_user_id="U_USER",
            bot_access_token="xoxb-user",
        )
        assert org_install.id != user_install.id
        assert dao.get_install_for_org(org.id).id == org_install.id
        assert dao.get_install_for_user(user.id).id == user_install.id

    def test_revoke_install_missing_returns_none(self, dbsession: Session) -> None:
        assert SlackDAO(dbsession).revoke_install(999999) is None

    def test_unbind_channel_missing_returns_false(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        assert (
            SlackDAO(dbsession).unbind_channel(
                slack_world["install"].id,
                "C_NEVER_BOUND",
            )
            is False
        )

    def test_delete_expired_routes_respects_explicit_cutoff(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = SlackDAO(dbsession)
        install = slack_world["install"]
        alex = slack_world["alex"]
        now = datetime.now(timezone.utc)

        soon = dao.upsert_thread_route(install.id, "C_SOON", "ts.1", alex.agent_id)
        soon.expires_at = now + timedelta(hours=1)
        later = dao.upsert_thread_route(install.id, "C_LATER", "ts.2", alex.agent_id)
        later.expires_at = now + timedelta(days=10)
        dbsession.flush()

        deleted = dao.delete_expired_routes(before=now + timedelta(hours=2))
        assert deleted == 1
        remaining = {
            r.channel_id
            for r in dbsession.query(SlackThreadRoute)
            .filter(SlackThreadRoute.install_id == install.id)
            .all()
        }
        assert remaining == {"C_LATER"}


# ============================================================================
# AssistantDAO additions
# ============================================================================


class TestCoordinatorAndTokenResolution:
    def test_coordinator_for_org_returns_the_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        coordinator = AssistantDAO(dbsession).coordinator(
            organization_id=slack_world["org"].id,
        )
        assert coordinator is not None
        assert coordinator.agent_id == slack_world["coordinator"].agent_id

    def test_coordinator_for_user_returns_the_personal_coordinator(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        coordinator = AssistantDAO(dbsession).coordinator(
            user_id=personal_slack_world["user"].id,
        )
        assert coordinator is not None
        assert coordinator.agent_id == personal_slack_world["coordinator"].agent_id

    def test_coordinator_for_org_returns_none_when_none_exists(
        self,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "no-coord")
        org = _make_org(dbsession, owner, "no-coord")
        _make_assistant(dbsession, owner, first_name="Solo", organization=org)
        assert AssistantDAO(dbsession).coordinator(organization_id=org.id) is None

    def test_coordinator_for_user_returns_none_when_none_exists(
        self,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "no-personal-coord")
        _make_assistant(dbsession, user, first_name="Solo", organization=None)
        assert AssistantDAO(dbsession).coordinator(user_id=user.id) is None

    def test_coordinator_requires_at_least_one_owner(self, dbsession: Session) -> None:
        dao = AssistantDAO(dbsession)
        with pytest.raises(ValueError):
            dao.coordinator()  # neither

    def test_coordinator_by_membership_scope(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``(user_id, organization_id)`` resolves that member's workspace
        Coordinator unambiguously."""
        coordinator = AssistantDAO(dbsession).coordinator(
            organization_id=slack_world["org"].id,
            user_id=slack_world["owner"].id,
        )
        assert coordinator is not None
        assert coordinator.agent_id == slack_world["coordinator"].agent_id

    def test_coordinator_org_scope_with_multiple_members_is_deterministic(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A second member's workspace Coordinator in the same org must not
        make the org-scoped lookup raise ``MultipleResultsFound``."""
        org = slack_world["org"]
        member2 = _make_user(dbsession, "member2")
        coord2 = _make_assistant(
            dbsession,
            member2,
            first_name="Cora",
            organization=org,
            is_coordinator=True,
        )
        result = AssistantDAO(dbsession).coordinator(organization_id=org.id)
        assert result is not None
        assert result.agent_id == min(
            slack_world["coordinator"].agent_id,
            coord2.agent_id,
        )

    def test_resolve_token_unique_case_insensitive(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = AssistantDAO(dbsession)
        for token in ("Alex", "alex", "ALEX", "  alex  "):
            matches = dao.resolve_token(
                token,
                organization_id=slack_world["org"].id,
            )
            assert len(matches) == 1
            assert matches[0].agent_id == slack_world["alex"].agent_id

    def test_resolve_token_unknown_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        matches = AssistantDAO(dbsession).resolve_token(
            "nonexistent",
            organization_id=slack_world["org"].id,
        )
        assert matches == []

    def test_resolve_token_ambiguous_returns_all_matches(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        org = slack_world["org"]
        # A second "Sara" — different creator (a fresh user) keeps the
        # workspace-scoped coordinator index out of play.
        another_owner = _make_user(dbsession, "another")
        _make_assistant(
            dbsession,
            another_owner,
            first_name="sara",
            organization=org,
        )

        matches = AssistantDAO(dbsession).resolve_token(
            "Sara",
            organization_id=org.id,
        )
        assert len(matches) == 2

    def test_resolve_token_empty_string_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        assert (
            AssistantDAO(dbsession).resolve_token(
                "",
                organization_id=slack_world["org"].id,
            )
            == []
        )

    def test_resolve_token_none_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Defensive: callers may pass through a missing-token sentinel."""
        assert (
            AssistantDAO(dbsession).resolve_token(
                None,  # type: ignore[arg-type]
                organization_id=slack_world["org"].id,
            )
            == []
        )

    def test_resolve_token_isolates_orgs(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """An assistant named 'Alex' in another org must not match this org's token."""
        other_owner = _make_user(dbsession, "other-org")
        other_org = _make_org(dbsession, other_owner, "other")
        _make_assistant(
            dbsession,
            other_owner,
            first_name="Alex",
            organization=other_org,
        )

        dao = AssistantDAO(dbsession)
        matches_here = dao.resolve_token("Alex", organization_id=slack_world["org"].id)
        assert len(matches_here) == 1
        assert matches_here[0].agent_id == slack_world["alex"].agent_id

        matches_there = dao.resolve_token("Alex", organization_id=other_org.id)
        assert len(matches_there) == 1
        assert matches_there[0].organization_id == other_org.id

    def test_resolve_token_for_user_finds_only_personal_assistants(
        self,
        dbsession: Session,
        personal_slack_world: dict,
        slack_world: dict,
    ) -> None:
        """Personal-mode resolution must not pick up org assistants.

        ``slack_world`` populates an org with an "Alex"; the personal
        resolution scope (``user_id`` + ``organization_id IS NULL``)
        must see only the personal "Alex" from ``personal_slack_world``.
        """
        user = personal_slack_world["user"]
        matches = AssistantDAO(dbsession).resolve_token("Alex", user_id=user.id)
        assert len(matches) == 1
        assert matches[0].agent_id == personal_slack_world["alex"].agent_id
        assert matches[0].organization_id is None
        assert matches[0].user_id == user.id

    def test_resolve_token_isolates_personal_users(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        """Two personal users can both have an 'Alex' without collision."""
        other_user = _make_user(dbsession, "other-personal")
        other_alex = _make_assistant(
            dbsession,
            other_user,
            first_name="Alex",
            organization=None,
        )

        dao = AssistantDAO(dbsession)
        mine = dao.resolve_token("Alex", user_id=personal_slack_world["user"].id)
        assert [a.agent_id for a in mine] == [personal_slack_world["alex"].agent_id]

        theirs = dao.resolve_token("Alex", user_id=other_user.id)
        assert [a.agent_id for a in theirs] == [other_alex.agent_id]

    def test_resolve_token_requires_owner_xor(
        self,
        dbsession: Session,
    ) -> None:
        dao = AssistantDAO(dbsession)
        with pytest.raises(ValueError):
            dao.resolve_token("alex")  # neither
        with pytest.raises(ValueError):
            dao.resolve_token("alex", organization_id=1, user_id="x")  # both

    def test_resolve_token_by_agent_id(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A numeric token resolves by ``agent_id`` (always unique)."""
        alex = slack_world["alex"]
        matches = AssistantDAO(dbsession).resolve_token(
            str(alex.agent_id),
            organization_id=slack_world["org"].id,
        )
        assert [a.agent_id for a in matches] == [alex.agent_id]

    def test_resolve_token_by_full_name(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``"first surname"`` (case-insensitive) resolves by full name.

        The fixture gives every assistant the surname ``"Bot"``.
        """
        dao = AssistantDAO(dbsession)
        # Case-insensitive, and surrounding whitespace is trimmed. The
        # matched form is the single-spaced ``"first surname"`` the
        # dispatcher constructs from the two leading words.
        for token in ("Alex Bot", "alex bot", "  ALEX BOT  "):
            matches = dao.resolve_token(token, organization_id=slack_world["org"].id)
            assert [a.agent_id for a in matches] == [slack_world["alex"].agent_id]

    def test_resolve_token_full_name_disambiguates_shared_first_name(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Two assistants share a first name; the full name picks exactly one."""
        org = slack_world["org"]
        another_owner = _make_user(dbsession, "alex-jones")
        alex_jones = Assistant(
            user_id=another_owner.id,
            organization_id=org.id,
            first_name="Alex",
            surname="Jones",
        )
        dbsession.add(alex_jones)
        dbsession.flush()

        dao = AssistantDAO(dbsession)
        # First name alone is now ambiguous …
        assert len(dao.resolve_token("Alex", organization_id=org.id)) == 2
        # … but the full names disambiguate.
        assert [
            a.agent_id for a in dao.resolve_token("Alex Bot", organization_id=org.id)
        ] == [
            slack_world["alex"].agent_id,
        ]
        assert [
            a.agent_id for a in dao.resolve_token("Alex Jones", organization_id=org.id)
        ] == [
            alex_jones.agent_id,
        ]

    def test_resolve_token_by_agent_id_is_owner_scoped(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """An agent id from another org must not resolve in this org's scope."""
        other_owner = _make_user(dbsession, "other-id-org")
        other_org = _make_org(dbsession, other_owner, "other-id")
        foreign = _make_assistant(
            dbsession,
            other_owner,
            first_name="Foreign",
            organization=other_org,
        )

        matches = AssistantDAO(dbsession).resolve_token(
            str(foreign.agent_id),
            organization_id=slack_world["org"].id,
        )
        assert matches == []

    def test_resolve_token_numeric_first_name_still_matches_by_name(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A digit-only first name resolves by name as well as colliding ids.

        ``isdigit()`` tokens additionally try ``agent_id``, but the name
        match must still fire so an assistant literally named ``"007"`` is
        reachable by that token.
        """
        org = slack_world["org"]
        agent_007 = _make_assistant(
            dbsession,
            slack_world["owner"],
            first_name="007",
            organization=org,
        )
        matches = AssistantDAO(dbsession).resolve_token("007", organization_id=org.id)
        assert agent_007.agent_id in {a.agent_id for a in matches}


# ============================================================================
# Dispatcher — DMs
# ============================================================================


def _dispatch(
    session: Session,
    *,
    team_id: str = "T01TEAM",
    channel: str,
    channel_type: str,
    sender: str = "U01ENDUSER",
    text: str,
    thread_ts: str | None = None,
    event_ts: str = "1700000000.000999",
    sender_email: str | None = None,
    sender_real_name: str | None = None,
    sender_display_name: str | None = None,
    sender_identity_provided: bool = False,
):
    return resolve_inbound(
        session,
        slack_team_id=team_id,
        channel_id=channel,
        channel_type=channel_type,
        sender_slack_user_id=sender,
        text=text,
        thread_ts=thread_ts,
        event_ts=event_ts,
        sender_email=sender_email,
        sender_real_name=sender_real_name,
        sender_display_name=sender_display_name,
        sender_identity_provided=sender_identity_provided,
    )


class TestDispatcherCommon:
    def test_unknown_team_returns_none(
        self,
        dbsession: Session,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            team_id="T_DOES_NOT_EXIST",
            channel="C_X",
            channel_type="channel",
            text="hi",
        )
        assert resolution is None

    def test_bot_echo_is_dropped_in_dm(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="D01",
            channel_type="im",
            sender="U01BOT",  # the install's bot user id
            text="hello",
        )
        assert resolution is None

    def test_bot_echo_is_dropped_in_channel(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """The bot's own outbound also fans back over the webhook — drop it."""
        # Even if the channel is bound, an echo of the bot's own message
        # must not be redelivered to an assistant.
        SlackDAO(dbsession).bind_channel(
            slack_world["install"].id,
            "C01TEAM",
            slack_world["alex"].agent_id,
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            sender="U01BOT",
            text="<@U01BOT> alex this is my own message",
        )
        assert resolution is None

    def test_mention_of_another_user_does_not_extract_token(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``<@SomeoneElse> alex`` is not addressed at our bot — no token.

        Routing should fall through to the default tree (binding /
        coordinator), not to the assistant named ``alex``.
        """
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U02HUMAN_TEAMMATE> alex check this",
        )
        assert resolution is not None
        # Initial DM, no bot mention extracted → coordinator handles it.
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.routing_metadata["reason"] == "initial_dm"

    def test_bot_mention_with_no_token_falls_through(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``<@U01BOT>`` alone isn't a token-addressed message."""
        SlackDAO(dbsession).bind_channel(
            slack_world["install"].id,
            "C01TEAM",
            slack_world["alex"].agent_id,
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT>   ",
        )
        # No token → behaves like an untokened mention → channel binding wins.
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.routing_metadata == {"reason": "channel_binding"}

    def test_bot_mention_not_at_start_is_ignored(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Token addressing requires the mention to be the first token.

        ``Hi <@bot> alex`` is conversational text, not addressing.
        """
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="Hi <@U01BOT> alex what's up",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.routing_metadata["reason"] == "initial_dm"


class TestDispatcherDM:
    def test_initial_dm_routes_to_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hey there",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.routing_metadata["reason"] == "initial_dm"
        assert resolution.route_persisted is False
        # Org install, no sender identity yet → provisional coordinator route
        # with a hint to re-dispatch for per-member precision.
        assert resolution.needs_sender_identity is True

    def test_dm_with_unique_token_pins_dm_route(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex what's up",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.thread_ts_for_route == DM_ROOT_SENTINEL
        assert resolution.route_persisted is True

        # Subsequent un-tokened message returns to Alex via the DM route.
        next_msg = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="follow up",
        )
        assert next_msg is not None
        assert next_msg.assistant_id == slack_world["alex"].agent_id

    def test_dm_handoff_via_token(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex begin",
        )
        # Same DM, new token → reassigns to Sara.
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> sara take over",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["sara"].agent_id
        next_msg = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="thanks",
        )
        assert next_msg is not None
        assert next_msg.assistant_id == slack_world["sara"].agent_id

    def test_dm_unknown_token_routes_to_coordinator_with_hint(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> bogus please reply",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.routing_metadata == {
            "reason": "unknown_token",
            "token": "bogus",
        }
        assert resolution.route_persisted is False

    def test_dm_route_resolution_refreshes_last_used_at(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Repeated DMs on a pinned route keep the route alive (sliding TTL)."""
        dao = SlackDAO(dbsession)
        _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex hi",
        )
        route = dao.get_dm_route(slack_world["install"].id, "D01HUMAN")
        assert route is not None
        original_last_used = route.last_used_at
        route.last_used_at = original_last_used - timedelta(hours=1)
        dbsession.flush()

        _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="next message",
        )
        refreshed = dao.get_dm_route(slack_world["install"].id, "D01HUMAN")
        assert refreshed is not None
        assert refreshed.last_used_at > original_last_used - timedelta(hours=1)

    def test_dm_ambiguous_token_routes_to_coordinator_with_candidates(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        # Add a second "Alex" in the same org.
        another = _make_user(dbsession, "second-alex")
        _make_assistant(
            dbsession,
            another,
            first_name="Alex",
            organization=slack_world["org"],
        )

        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex hello",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        meta = resolution.routing_metadata
        assert meta["reason"] == "ambiguous_token"
        assert meta["token"] == "alex"
        names = {c["first_name"] for c in meta["candidates"]}
        assert names == {"Alex"}
        assert len(meta["candidates"]) == 2

    def test_dm_full_name_resolves_when_first_name_is_ambiguous(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``@app First Last`` routes to one assistant even when first names clash."""
        org = slack_world["org"]
        another_owner = _make_user(dbsession, "alex-jones-dm")
        alex_jones = Assistant(
            user_id=another_owner.id,
            organization_id=org.id,
            first_name="Alex",
            surname="Jones",
        )
        dbsession.add(alex_jones)
        dbsession.flush()

        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> Alex Jones please help",
        )
        assert resolution is not None
        assert resolution.assistant_id == alex_jones.agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "Alex Jones",
        }

    def test_dm_agent_id_addresses_assistant(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``@app <agent_id>`` routes by numeric id (the guaranteed disambiguator)."""
        alex = slack_world["alex"]
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text=f"<@U01BOT> {alex.agent_id} hello there",
        )
        assert resolution is not None
        assert resolution.assistant_id == alex.agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": str(alex.agent_id),
        }

    def test_dm_full_name_falls_back_to_first_name_when_no_surname_match(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A trailing word that isn't the surname is treated as message text.

        ``@app alex hello`` has two words but ``"alex hello"`` is not a
        full name, so resolution falls back to the leading first-name word.
        """
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex hello",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "alex",
        }

    def test_dm_first_name_with_trailing_comma_resolves(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Punctuation attached to the token (``@app alex, …``) is stripped."""
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> alex, can you help",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "alex",
        }

    def test_dm_full_name_with_trailing_comma_resolves(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``@app First Last, …`` resolves despite the comma after the surname."""
        org = slack_world["org"]
        another_owner = _make_user(dbsession, "alex-jones-comma-dm")
        alex_jones = Assistant(
            user_id=another_owner.id,
            organization_id=org.id,
            first_name="Alex",
            surname="Jones",
        )
        dbsession.add(alex_jones)
        dbsession.flush()

        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="<@U01BOT> Alex Jones, please help",
        )
        assert resolution is not None
        assert resolution.assistant_id == alex_jones.agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "Alex Jones",
        }


class TestDispatcherPersonalInstall:
    """Personal-mode dispatcher: routing scoped to a single Unify user.

    Mirrors the org-mode DM tests but the bot identity (``U02BOT``) and
    workspace (``T02PERSONAL``) come from the personal install fixture.
    The dispatcher must derive ``user_id`` scope from the install and
    only route to personal assistants of that user.
    """

    def test_initial_dm_routes_to_personal_coordinator(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            team_id="T02PERSONAL",
            channel="D02HUMAN",
            channel_type="im",
            text="hey",
        )
        assert resolution is not None
        assert resolution.assistant_id == personal_slack_world["coordinator"].agent_id
        assert resolution.routing_metadata["reason"] == "initial_dm"
        assert resolution.install.user_id == personal_slack_world["user"].id
        assert resolution.install.organization_id is None
        # A personal install has exactly one Coordinator, so the sender's
        # identity is irrelevant — never ask the gateway to re-dispatch.
        assert resolution.needs_sender_identity is False

    def test_dm_token_routes_to_personal_assistant(
        self,
        dbsession: Session,
        personal_slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            team_id="T02PERSONAL",
            channel="D02HUMAN",
            channel_type="im",
            text="<@U02BOT> alex hi",
        )
        assert resolution is not None
        assert resolution.assistant_id == personal_slack_world["alex"].agent_id
        assert resolution.thread_ts_for_route == DM_ROOT_SENTINEL

        # Subsequent un-tokened DM stays with Alex via the pinned route.
        follow_up = _dispatch(
            dbsession,
            team_id="T02PERSONAL",
            channel="D02HUMAN",
            channel_type="im",
            text="follow up",
        )
        assert follow_up is not None
        assert follow_up.assistant_id == personal_slack_world["alex"].agent_id

    def test_personal_dm_does_not_resolve_org_token(
        self,
        dbsession: Session,
        personal_slack_world: dict,
        slack_world: dict,
    ) -> None:
        """Token resolution must not leak across owner scopes.

        ``slack_world`` adds an "Alex" inside an org. The personal-mode
        dispatcher must not see it — only the user's personal Alex. We
        verify by adding a third assistant in the org that doesn't exist
        in the personal world ("Sara") and confirming the personal
        dispatch falls back to the personal coordinator with an
        unknown-token hint.
        """
        # Sanity: there *is* a personal Sara; we want a token that exists
        # only in the org, so use a fresh name.
        owner_user = slack_world["owner"]
        _make_assistant(
            dbsession,
            owner_user,
            first_name="Onlyorg",
            organization=slack_world["org"],
        )
        resolution = _dispatch(
            dbsession,
            team_id="T02PERSONAL",
            channel="D02HUMAN",
            channel_type="im",
            text="<@U02BOT> onlyorg please",
        )
        assert resolution is not None
        assert resolution.assistant_id == personal_slack_world["coordinator"].agent_id
        assert resolution.routing_metadata == {
            "reason": "unknown_token",
            "token": "onlyorg",
        }


# ============================================================================
# Dispatcher — channels
# ============================================================================


class TestDispatcherChannel:
    def test_top_level_token_pins_thread_route_using_event_ts(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara look at this",
            event_ts="1700000000.111111",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["sara"].agent_id
        assert resolution.thread_ts_for_route == "1700000000.111111"
        assert resolution.route_persisted is True

        # In-thread reply (no token, thread_ts == the original event_ts)
        # inherits the route.
        follow_up = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="quick follow up",
            thread_ts="1700000000.111111",
            event_ts="1700000000.222222",
        )
        assert follow_up is not None
        assert follow_up.assistant_id == slack_world["sara"].agent_id

    def test_in_thread_token_hands_off(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> alex initial",
            event_ts="1700000001.000000",
        )
        # Same thread, new token → hand off to Sara.
        handoff = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara now you",
            thread_ts="1700000001.000000",
            event_ts="1700000001.111111",
        )
        assert handoff is not None
        assert handoff.assistant_id == slack_world["sara"].agent_id
        assert handoff.thread_ts_for_route == "1700000001.000000"

        # Subsequent un-tokened message stays with Sara.
        follow_up = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="ok",
            thread_ts="1700000001.000000",
            event_ts="1700000001.222222",
        )
        assert follow_up is not None
        assert follow_up.assistant_id == slack_world["sara"].agent_id

    def test_channel_binding_used_for_top_level_untokened(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        SlackDAO(dbsession).bind_channel(
            slack_world["install"].id,
            "C01TEAM",
            slack_world["alex"].agent_id,
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="just a normal message",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.routing_metadata == {"reason": "channel_binding"}
        assert resolution.route_persisted is False

    def test_no_binding_no_route_no_token_returns_none(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="C_QUIET",
            channel_type="channel",
            text="just chatter",
        )
        assert resolution is None

    def test_channel_ambiguous_token_routes_to_coordinator_in_thread(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        another = _make_user(dbsession, "second-sara")
        _make_assistant(
            dbsession,
            another,
            first_name="Sara",
            organization=slack_world["org"],
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara help",
            event_ts="1700000003.000000",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.routing_metadata["reason"] == "ambiguous_token"
        # Coordinator gets the thread so it can clarify in place. The
        # provisional route is persisted even though identity is pending.
        assert resolution.route_persisted is True
        assert resolution.thread_ts_for_route == "1700000003.000000"
        assert resolution.needs_sender_identity is True

    def test_channel_token_addressed_metadata(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> ALEX status report",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "ALEX",
        }

    def test_in_thread_untokened_without_route_falls_through_to_binding(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        SlackDAO(dbsession).bind_channel(
            slack_world["install"].id,
            "C01TEAM",
            slack_world["alex"].agent_id,
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="reviving an old thread",
            thread_ts="1700000005.000000",
            event_ts="1700000005.999999",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["alex"].agent_id
        # Did not auto-persist because the in-thread message was un-tokened.
        assert resolution.route_persisted is False

    def test_in_thread_untokened_with_no_route_no_binding_returns_none(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A stray thread reply with no live route and no binding is ignored."""
        resolution = _dispatch(
            dbsession,
            channel="C_QUIET",
            channel_type="channel",
            text="reviving a stale thread",
            thread_ts="1700000006.000000",
            event_ts="1700000006.999999",
        )
        assert resolution is None

    def test_token_overrides_existing_channel_binding(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A bound channel must still honor explicit token addressing.

        Alex is the default for ``C01TEAM`` but the user explicitly tags
        Sara, so Sara handles this thread.
        """
        SlackDAO(dbsession).bind_channel(
            slack_world["install"].id,
            "C01TEAM",
            slack_world["alex"].agent_id,
        )
        resolution = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara please weigh in",
            event_ts="1700000007.000000",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["sara"].agent_id
        assert resolution.routing_metadata == {
            "reason": "token_addressed",
            "token": "sara",
        }
        assert resolution.route_persisted is True

        # Subsequent in-thread reply with no token goes to Sara (route wins
        # over the channel binding).
        follow_up = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="ack",
            thread_ts="1700000007.000000",
            event_ts="1700000007.111111",
        )
        assert follow_up is not None
        assert follow_up.assistant_id == slack_world["sara"].agent_id

    def test_inheriting_thread_route_refreshes_last_used_at(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Sliding TTL: every in-thread hit must move ``last_used_at`` forward."""
        dao = SlackDAO(dbsession)
        _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> alex starting",
            event_ts="1700000008.000000",
        )
        route = dao.get_thread_route(
            slack_world["install"].id,
            "C01TEAM",
            "1700000008.000000",
        )
        assert route is not None
        # Backdate so the refresh produces a strictly newer timestamp.
        original_last_used = route.last_used_at
        route.last_used_at = original_last_used - timedelta(hours=1)
        dbsession.flush()

        _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="follow up",
            thread_ts="1700000008.000000",
            event_ts="1700000008.111111",
        )
        refreshed = dao.get_thread_route(
            slack_world["install"].id,
            "C01TEAM",
            "1700000008.000000",
        )
        assert refreshed is not None
        assert refreshed.last_used_at > original_last_used - timedelta(hours=1)


# ============================================================================
# Dispatcher — coordinator sender identity (two-phase resolution)
# ============================================================================


class TestDispatcherCoordinatorIdentity:
    """The default (coordinator) path is *personal to the sender* in an org.

    Phase 1 (no identity) routes provisionally to the org's deterministic
    Coordinator and asks the caller to re-dispatch. Phase 2 (identity
    provided) pins the message to the sender's *own* workspace Coordinator,
    matching the sender to the member who owns one by email or name.
    """

    def _add_member_with_coordinator(
        self,
        dbsession: Session,
        org: Organization,
        *,
        suffix: str,
        first_name: str,
        email: str | None = None,
        last_name: str | None = None,
    ) -> tuple[User, Assistant]:
        member = _make_user(dbsession, suffix)
        if email is not None:
            member.email = email
        if last_name is not None:
            member.last_name = last_name
        dbsession.flush()
        coordinator = _make_assistant(
            dbsession,
            member,
            first_name=first_name,
            organization=org,
            is_coordinator=True,
        )
        return member, coordinator

    def test_phase_one_dm_is_provisional_and_requests_identity(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hello there",
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.needs_sender_identity is True
        # Provisional → not pinned, so the second pass can re-route.
        assert resolution.route_persisted is False
        assert (
            SlackDAO(dbsession).get_dm_route(slack_world["install"].id, "D01HUMAN")
            is None
        )

    def test_phase_two_dm_email_pins_senders_own_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        org = slack_world["org"]
        member, member_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="cora-owner",
            first_name="Cora",
            email="cora@member.test",
        )
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hello there",
            sender_email="cora@member.test",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == member_coordinator.agent_id
        assert resolution.assistant_id != slack_world["coordinator"].agent_id
        assert resolution.needs_sender_identity is False
        # Final → DM route pinned to the sender's own Coordinator.
        assert resolution.route_persisted is True
        route = SlackDAO(dbsession).get_dm_route(
            slack_world["install"].id,
            "D01HUMAN",
        )
        assert route is not None
        assert route.assistant_id == member_coordinator.agent_id

    def test_phase_two_dm_name_match_pins_senders_own_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        org = slack_world["org"]
        member, member_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="dana-owner",
            first_name="Dana",
            email="dana@member.test",
            last_name="Scully",
        )
        # No email on the event — fall back to the real_name match.
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_real_name=f"{member.name} Scully",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == member_coordinator.agent_id
        assert resolution.needs_sender_identity is False

    def test_phase_two_unknown_sender_falls_back_to_org_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """An identity that matches no member routes to the deterministic
        org Coordinator rather than dropping."""
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hello there",
            sender_email="stranger@example.com",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.needs_sender_identity is False
        assert resolution.route_persisted is True

    def test_phase_two_routes_distinct_members_to_distinct_coordinators(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        org = slack_world["org"]
        _, coord_a = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="member-a",
            first_name="Aria",
            email="a@member.test",
        )
        _, coord_b = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="member-b",
            first_name="Bram",
            email="b@member.test",
        )
        res_a = _dispatch(
            dbsession,
            channel="D0A",
            channel_type="im",
            text="hi",
            sender_email="a@member.test",
            sender_identity_provided=True,
        )
        res_b = _dispatch(
            dbsession,
            channel="D0B",
            channel_type="im",
            text="hi",
            sender_email="b@member.test",
            sender_identity_provided=True,
        )
        assert res_a is not None and res_b is not None
        assert res_a.assistant_id == coord_a.agent_id
        assert res_b.assistant_id == coord_b.agent_id
        assert res_a.assistant_id != res_b.assistant_id

    def test_phase_two_channel_repins_thread_to_member_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """A second-pass channel dispatch re-pins the thread route from the
        provisional org Coordinator to the sender's own Coordinator."""
        org = slack_world["org"]
        # Two "Sara"s make the token ambiguous → coordinator fallback.
        another = _make_user(dbsession, "second-sara-id")
        _make_assistant(
            dbsession,
            another,
            first_name="Sara",
            organization=org,
        )
        _, member_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="evan-owner",
            first_name="Evan",
            email="evan@member.test",
        )
        dao = SlackDAO(dbsession)

        phase_one = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara help",
            event_ts="1700009000.000000",
        )
        assert phase_one is not None
        assert phase_one.needs_sender_identity is True
        assert phase_one.assistant_id == slack_world["coordinator"].agent_id
        route = dao.get_thread_route(
            slack_world["install"].id,
            "C01TEAM",
            "1700009000.000000",
        )
        assert route is not None
        assert route.assistant_id == slack_world["coordinator"].agent_id

        phase_two = _dispatch(
            dbsession,
            channel="C01TEAM",
            channel_type="channel",
            text="<@U01BOT> sara help",
            event_ts="1700009000.000000",
            sender_email="evan@member.test",
            sender_identity_provided=True,
        )
        assert phase_two is not None
        assert phase_two.needs_sender_identity is False
        assert phase_two.assistant_id == member_coordinator.agent_id
        repinned = dao.get_thread_route(
            slack_world["install"].id,
            "C01TEAM",
            "1700009000.000000",
        )
        assert repinned is not None
        assert repinned.assistant_id == member_coordinator.agent_id

    def test_org_install_without_coordinator_drops_gracefully(
        self,
        dbsession: Session,
    ) -> None:
        """No Coordinator anywhere in the org → drop (no crash, no 500)."""
        owner = _make_user(dbsession, "no-coord-owner")
        org = _make_org(dbsession, owner, "no-coord")
        _make_assistant(dbsession, owner, first_name="Alex", organization=org)
        _make_install(
            dbsession,
            organization=org,
            slack_team_id="T_NO_COORD",
            bot_user_id="U_NO_COORD_BOT",
        )
        resolution = _dispatch(
            dbsession,
            team_id="T_NO_COORD",
            channel="D_NO_COORD",
            channel_type="im",
            text="anyone home?",
        )
        assert resolution is None

    def test_phase_two_ambiguous_name_match_falls_back_to_org_coordinator(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Two members sharing a name make a name match ambiguous.

        We never guess between them — an ambiguous match is refused and the
        sender routes to the deterministic org Coordinator instead of the
        wrong person's workspace Coordinator.
        """
        org = slack_world["org"]
        first, _ = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="twin-a",
            first_name="TwinA",
        )
        second, _ = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="twin-b",
            first_name="TwinB",
        )
        # Collide their profile names so the sender name matches both.
        for member in (first, second):
            member.name = "Sam"
            member.last_name = "Rivers"
        dbsession.flush()

        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_real_name="Sam Rivers",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.needs_sender_identity is False

    def test_phase_two_empty_identity_is_loop_safe(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """``provided=True`` with no email/name (users.info returned nothing).

        The gateway still marks identity as provided so routing cannot loop:
        we fall back to the deterministic org Coordinator with
        ``needs_sender_identity=False``, never asking for identity again.
        """
        # A member with a Coordinator exists, so owners are non-empty; the
        # empty identity must still fail to match and fall back.
        self._add_member_with_coordinator(
            dbsession,
            org=slack_world["org"],
            suffix="present-member",
            first_name="Present",
            email="present@member.test",
        )
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == slack_world["coordinator"].agent_id
        assert resolution.needs_sender_identity is False
        assert resolution.route_persisted is True

    def test_phase_two_name_match_is_accent_case_and_order_insensitive(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Name matching normalizes accents/case and tries either ordering."""
        org = slack_world["org"]
        member, member_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="jose-owner",
            first_name="Jose",
            last_name="García",
        )
        member.name = "José"
        dbsession.flush()

        # Reversed order, lower-cased, accents stripped — still resolves.
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_real_name="garcia jose",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == member_coordinator.agent_id
        assert resolution.needs_sender_identity is False

    def test_phase_two_email_wins_over_conflicting_name(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Email is the top of the matching ladder, ahead of a name match."""
        org = slack_world["org"]
        email_member, email_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="by-email",
            first_name="Mailer",
            email="match@member.test",
        )
        name_member, _name_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="by-name",
            first_name="Named",
        )
        name_member.name = "Distinct"
        name_member.last_name = "Person"
        dbsession.flush()

        # Email points at one member, real_name at the other — email wins.
        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_email="match@member.test",
            sender_real_name="Distinct Person",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == email_coordinator.agent_id

    def test_phase_two_display_name_match_when_real_name_absent(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """The name ladder falls through real_name to display_name."""
        org = slack_world["org"]
        member, member_coordinator = self._add_member_with_coordinator(
            dbsession,
            org,
            suffix="display-owner",
            first_name="Disp",
            last_name="Lay",
        )
        member.name = "Disp"
        dbsession.flush()

        resolution = _dispatch(
            dbsession,
            channel="D01HUMAN",
            channel_type="im",
            text="hi",
            sender_display_name="Disp Lay",
            sender_identity_provided=True,
        )
        assert resolution is not None
        assert resolution.assistant_id == member_coordinator.agent_id
        assert resolution.needs_sender_identity is False


# ============================================================================
# Admin HTTP endpoints
# ============================================================================


@pytest.mark.anyio
class TestAdminEndpoints:
    async def test_install_upsert_and_get(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "http-install")
        org = _make_org(dbsession, owner, "http")
        dbsession.commit()

        payload = {
            "organization_id": org.id,
            "slack_team_id": "T_HTTP",
            "slack_app_id": "A_HTTP",
            "bot_user_id": "U_HTTP_BOT",
            "bot_access_token": "xoxb-http",
            "slack_team_name": "HTTP Workspace",
        }
        resp = await client.post(
            "/v0/admin/slack/install",
            json=payload,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["organization_id"] == org.id
        assert body["bot_user_id"] == "U_HTTP_BOT"
        assert body["bot_access_token"] is None  # not requested on create

        get_resp = await client.get(
            "/v0/admin/slack/install",
            params={"slack_team_id": "T_HTTP", "include_token": True},
            headers=ADMIN_HEADERS,
        )
        assert get_resp.status_code == status.HTTP_200_OK
        assert get_resp.json()["bot_access_token"] == "xoxb-http"

        get_by_org = await client.get(
            "/v0/admin/slack/install",
            params={"organization_id": org.id},
            headers=ADMIN_HEADERS,
        )
        assert get_by_org.status_code == status.HTTP_200_OK
        assert get_by_org.json()["slack_team_id"] == "T_HTTP"

    async def test_install_upsert_reawakens_org_assistants(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "reawaken-org")
        org = _make_org(dbsession, owner, "reawaken-org")
        coordinator = _make_assistant(
            dbsession,
            owner,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        member = _make_assistant(
            dbsession,
            owner,
            first_name="Member",
            organization=org,
        )
        # A personal assistant of the same owner must not be reawakened
        # for an org install.
        personal = _make_assistant(
            dbsession,
            owner,
            first_name="Solo",
            organization=None,
        )
        dbsession.commit()

        with patch(
            "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
            new_callable=AsyncMock,
        ) as mock_reawaken:
            resp = await client.post(
                "/v0/admin/slack/install",
                json={
                    "organization_id": org.id,
                    "slack_team_id": "T_REAWAKEN_ORG",
                    "slack_app_id": "A_REAWAKEN",
                    "bot_user_id": "U_REAWAKEN_BOT",
                    "bot_access_token": "xoxb-reawaken",
                },
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == status.HTTP_200_OK
        reawakened = {call.args[0] for call in mock_reawaken.call_args_list}
        assert reawakened == {
            str(coordinator.agent_id),
            str(member.agent_id),
        }
        assert str(personal.agent_id) not in reawakened

    async def test_install_upsert_stamps_installer_slack_id_on_initiator_org_assistants(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        installer = _make_user(dbsession, "slack-stamp-installer")
        other_member = _make_user(dbsession, "slack-stamp-member")
        org = _make_org(dbsession, installer, "slack-stamp")
        _make_assistants_project(dbsession, organization=org)
        _make_assistants_project(dbsession, user=installer)

        org_assistant = _make_assistant(
            dbsession,
            installer,
            first_name="OrgTwin",
            organization=org,
        )
        personal_assistant = _make_assistant(
            dbsession,
            installer,
            first_name="SoloTwin",
            organization=None,
        )
        member_assistant = _make_assistant(
            dbsession,
            other_member,
            first_name="MemberTwin",
            organization=org,
        )

        ensure_owner_contact_row(dbsession, assistant=org_assistant)
        ensure_owner_contact_row(dbsession, assistant=personal_assistant)
        ensure_owner_contact_row(dbsession, assistant=member_assistant)
        dbsession.commit()

        with (
            patch(
                "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
                new_callable=AsyncMock,
            ),
            patch(
                "orchestra.web.api.utils.assistant_infra._trigger_contact_sync",
                new_callable=AsyncMock,
            ) as mock_contact_sync,
        ):
            resp = await client.post(
                "/v0/admin/slack/install",
                json={
                    "organization_id": org.id,
                    "slack_team_id": "T_STAMP_ORG",
                    "slack_app_id": "A_STAMP",
                    "bot_user_id": "U_STAMP_BOT",
                    "bot_access_token": "xoxb-stamp",
                    "installer_user_id": "U_INSTALLER",
                    "initiator_user_id": installer.id,
                },
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == status.HTTP_200_OK

        org_boss = _boss_contact_data(dbsession, assistant=org_assistant)
        assert org_boss is not None
        assert org_boss.get("slack_user_id") == "U_INSTALLER"

        personal_boss = _boss_contact_data(dbsession, assistant=personal_assistant)
        assert personal_boss is not None
        assert not personal_boss.get("slack_user_id")

        member_boss = _boss_contact_data(dbsession, assistant=member_assistant)
        assert member_boss is not None
        assert not member_boss.get("slack_user_id")

        synced = {call.args[0] for call in mock_contact_sync.call_args_list}
        assert synced == {org_assistant.agent_id}

    async def test_install_upsert_reawakens_personal_assistants(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "reawaken-personal")
        org = _make_org(dbsession, owner, "reawaken-personal")
        personal = _make_assistant(
            dbsession,
            owner,
            first_name="Solo",
            organization=None,
        )
        # An org assistant of the same owner must not be reawakened for a
        # personal install.
        org_assistant = _make_assistant(
            dbsession,
            owner,
            first_name="OrgBot",
            organization=org,
        )
        dbsession.commit()

        with patch(
            "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
            new_callable=AsyncMock,
        ) as mock_reawaken:
            resp = await client.post(
                "/v0/admin/slack/install",
                json={
                    "user_id": owner.id,
                    "slack_team_id": "T_REAWAKEN_USER",
                    "slack_app_id": "A_REAWAKEN",
                    "bot_user_id": "U_REAWAKEN_BOT",
                    "bot_access_token": "xoxb-reawaken",
                },
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == status.HTTP_200_OK
        reawakened = {call.args[0] for call in mock_reawaken.call_args_list}
        assert reawakened == {str(personal.agent_id)}
        assert str(org_assistant.agent_id) not in reawakened

    async def test_install_upsert_survives_reawaken_failure(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "reawaken-fail")
        _make_assistant(
            dbsession,
            owner,
            first_name="Solo",
            organization=None,
        )
        dbsession.commit()

        with patch(
            "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
            new_callable=AsyncMock,
            side_effect=RuntimeError("adapters down"),
        ):
            resp = await client.post(
                "/v0/admin/slack/install",
                json={
                    "user_id": owner.id,
                    "slack_team_id": "T_REAWAKEN_FAIL",
                    "slack_app_id": "A_REAWAKEN",
                    "bot_user_id": "U_REAWAKEN_BOT",
                    "bot_access_token": "xoxb-reawaken",
                },
                headers=ADMIN_HEADERS,
            )
        assert resp.status_code == status.HTTP_200_OK

    async def test_installs_list_returns_all_without_tokens(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "http-list")
        org = _make_org(dbsession, owner, "http-list")
        personal = _make_user(dbsession, "http-list-personal")
        dbsession.commit()

        await client.post(
            "/v0/admin/slack/install",
            json={
                "organization_id": org.id,
                "slack_team_id": "T_LIST_ORG",
                "slack_app_id": "A_LIST",
                "bot_user_id": "U_LIST_ORG",
                "bot_access_token": "xoxb-list-org",
            },
            headers=ADMIN_HEADERS,
        )
        await client.post(
            "/v0/admin/slack/install",
            json={
                "user_id": personal.id,
                "slack_team_id": "T_LIST_USER",
                "slack_app_id": "A_LIST",
                "bot_user_id": "U_LIST_USER",
                "bot_access_token": "xoxb-list-user",
            },
            headers=ADMIN_HEADERS,
        )

        resp = await client.get("/v0/admin/slack/installs", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        by_team = {row["slack_team_id"]: row for row in rows}
        assert {"T_LIST_ORG", "T_LIST_USER"} <= set(by_team)
        assert by_team["T_LIST_ORG"]["organization_id"] == org.id
        assert by_team["T_LIST_USER"]["user_id"] == personal.id
        # List never leaks bot tokens.
        assert all(row["bot_access_token"] is None for row in rows)

    async def test_install_get_404_when_missing(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.get(
            "/v0/admin/slack/install",
            params={"slack_team_id": "T_GHOST"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_install_get_requires_exactly_one_selector(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()

        no_args = await client.get("/v0/admin/slack/install", headers=ADMIN_HEADERS)
        assert no_args.status_code == status.HTTP_400_BAD_REQUEST

        two_args = await client.get(
            "/v0/admin/slack/install",
            params={
                "slack_team_id": "T01TEAM",
                "organization_id": slack_world["org"].id,
            },
            headers=ADMIN_HEADERS,
        )
        assert two_args.status_code == status.HTTP_400_BAD_REQUEST

    async def test_install_upsert_personal_and_get_by_user(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-personal")
        dbsession.commit()

        payload = {
            "user_id": user.id,
            "slack_team_id": "T_PERSONAL_HTTP",
            "slack_app_id": "A_HTTP",
            "bot_user_id": "U_HTTP_BOT",
            "bot_access_token": "xoxb-personal",
            "slack_team_name": "Personal Workspace",
        }
        upsert = await client.post(
            "/v0/admin/slack/install",
            json=payload,
            headers=ADMIN_HEADERS,
        )
        assert upsert.status_code == status.HTTP_200_OK
        body = upsert.json()
        assert body["user_id"] == user.id
        assert body["organization_id"] is None

        by_user = await client.get(
            "/v0/admin/slack/install",
            params={"user_id": user.id, "include_token": True},
            headers=ADMIN_HEADERS,
        )
        assert by_user.status_code == status.HTTP_200_OK
        assert by_user.json()["bot_access_token"] == "xoxb-personal"

    async def test_install_upsert_rejects_both_owners(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-both")
        org = _make_org(dbsession, user, "http-both")
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/slack/install",
            json={
                "organization_id": org.id,
                "user_id": user.id,
                "slack_team_id": "T_BOTH_HTTP",
                "slack_app_id": "A_HTTP",
                "bot_user_id": "U_HTTP",
                "bot_access_token": "xoxb-both",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    async def test_install_upsert_rejects_no_owner(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/v0/admin/slack/install",
            json={
                "slack_team_id": "T_NONE_HTTP",
                "slack_app_id": "A_HTTP",
                "bot_user_id": "U_HTTP",
                "bot_access_token": "xoxb-none",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    async def test_revoke_install(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()
        install_id = slack_world["install"].id
        resp = await client.delete(
            f"/v0/admin/slack/install/{install_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        # No Slack app credentials configured in the test env, so the
        # provider-side uninstall is skipped and this degrades to a
        # local soft-revoke.
        assert resp.json() == {
            "id": install_id,
            "revoked": True,
            "uninstalled": False,
        }

        follow = await client.get(
            "/v0/admin/slack/install",
            params={"slack_team_id": "T01TEAM"},
            headers=ADMIN_HEADERS,
        )
        assert follow.status_code == status.HTTP_404_NOT_FOUND

    async def test_revoke_install_uninstalls_when_configured(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With client credentials set, revoke calls Slack ``apps.uninstall``
        (with the workspace bot token) and reports it uninstalled."""
        dbsession.commit()
        install_id = slack_world["install"].id

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_id",
            "client-id",
        )
        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_secret",
            "client-secret",
        )
        calls: list[dict] = []

        async def _fake_uninstall(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.uninstall_slack_app",
            _fake_uninstall,
        )

        resp = await client.delete(
            f"/v0/admin/slack/install/{install_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {
            "id": install_id,
            "revoked": True,
            "uninstalled": True,
        }
        assert calls == [
            {
                "bot_token": "xoxb-test",
                "client_id": "client-id",
                "client_secret": "client-secret",
            },
        ]

        follow = await client.get(
            "/v0/admin/slack/install",
            params={"slack_team_id": "T01TEAM"},
            headers=ADMIN_HEADERS,
        )
        assert follow.status_code == status.HTTP_404_NOT_FOUND

    async def test_revoke_install_skips_uninstall_when_unconfigured(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without client credentials, Slack is never contacted; the install
        is still soft-revoked."""
        dbsession.commit()
        install_id = slack_world["install"].id

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_id",
            None,
        )
        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_secret",
            None,
        )
        called = False

        async def _fake_uninstall(**_kwargs: object) -> bool:
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.uninstall_slack_app",
            _fake_uninstall,
        )

        resp = await client.delete(
            f"/v0/admin/slack/install/{install_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {
            "id": install_id,
            "revoked": True,
            "uninstalled": False,
        }
        assert called is False

    async def test_revoke_install_soft_revokes_when_uninstall_fails(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed provider-side uninstall must not block the local revoke."""
        dbsession.commit()
        install_id = slack_world["install"].id

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_id",
            "client-id",
        )
        monkeypatch.setattr(
            "orchestra.web.api.slack.views.settings.slack_client_secret",
            "client-secret",
        )

        async def _fake_uninstall(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.uninstall_slack_app",
            _fake_uninstall,
        )

        resp = await client.delete(
            f"/v0/admin/slack/install/{install_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {
            "id": install_id,
            "revoked": True,
            "uninstalled": False,
        }

        follow = await client.get(
            "/v0/admin/slack/install",
            params={"slack_team_id": "T01TEAM"},
            headers=ADMIN_HEADERS,
        )
        assert follow.status_code == status.HTTP_404_NOT_FOUND

    async def test_dispatch_routes_to_coordinator_for_initial_dm(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()
        payload = {
            "slack_team_id": "T01TEAM",
            "channel_id": "D01HUMAN",
            "channel_type": "im",
            "sender_slack_user_id": "U01HUMAN",
            "text": "hello assistant!",
            "thread_ts": None,
            "event_ts": "1700000010.000001",
        }
        resp = await client.post(
            "/v0/admin/slack/dispatch",
            json=payload,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["handled"] is True
        assert body["assistant_id"] == slack_world["coordinator"].agent_id
        assert body["routing_metadata"]["reason"] == "initial_dm"
        assert body["bot_user_id"] == "U01BOT"

    async def test_dispatch_handled_false_when_no_install(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/v0/admin/slack/dispatch",
            json={
                "slack_team_id": "T_NONE",
                "channel_id": "C_X",
                "channel_type": "channel",
                "sender_slack_user_id": "U_X",
                "text": "hi",
                "event_ts": "1700000020.000001",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {
            "handled": False,
            "install_id": None,
            "organization_id": None,
            "user_id": None,
            "assistant_id": None,
            "bot_user_id": None,
            "thread_ts_for_route": None,
            "route_persisted": False,
            "routing_metadata": {},
            "needs_sender_identity": False,
        }

    async def test_dispatch_routing_fault_degrades_to_handled_false(
        self,
        client: AsyncClient,
        slack_world: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A routing fault must surface as ``handled=false``, never a 500.

        Slack retries 5xx responses aggressively, so an unexpected exception
        in resolution would amplify a single transient fault into a redelivery
        storm. The endpoint catches it, logs the traceback, and tells the
        gateway to drop the event.
        """

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated routing fault")

        monkeypatch.setattr(
            "orchestra.web.api.slack.views.resolve_inbound",
            _boom,
        )
        resp = await client.post(
            "/v0/admin/slack/dispatch",
            json={
                "slack_team_id": slack_world["install"].slack_team_id,
                "channel_id": "D01HUMAN",
                "channel_type": "im",
                "sender_slack_user_id": "U_HUMAN",
                "text": "hi",
                "event_ts": "1700000099.000001",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["handled"] is False

    async def test_channel_binding_lifecycle(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()
        install_id = slack_world["install"].id
        alex_id = slack_world["alex"].agent_id

        create = await client.post(
            "/v0/admin/slack/channel-bindings",
            json={
                "install_id": install_id,
                "channel_id": "C_CHAN",
                "assistant_id": alex_id,
                "channel_name": "design",
            },
            headers=ADMIN_HEADERS,
        )
        assert create.status_code == status.HTTP_200_OK
        assert create.json()["assistant_id"] == alex_id

        listed = await client.get(
            "/v0/admin/slack/channel-bindings",
            params={"install_id": install_id},
            headers=ADMIN_HEADERS,
        )
        assert listed.status_code == status.HTTP_200_OK
        rows = listed.json()
        assert any(
            r["channel_id"] == "C_CHAN" and r["assistant_id"] == alex_id for r in rows
        )

        deleted = await client.delete(
            "/v0/admin/slack/channel-bindings",
            params={"install_id": install_id, "channel_id": "C_CHAN"},
            headers=ADMIN_HEADERS,
        )
        assert deleted.status_code == status.HTTP_200_OK
        assert deleted.json() == {"deleted": True}

    async def test_delete_missing_channel_binding_returns_false(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()
        resp = await client.delete(
            "/v0/admin/slack/channel-bindings",
            params={
                "install_id": slack_world["install"].id,
                "channel_id": "C_NEVER_BOUND",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {"deleted": False}

    async def test_dispatch_token_addressed_channel_returns_full_routing(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """All five routing fields surface on a happy-path token dispatch."""
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/slack/dispatch",
            json={
                "slack_team_id": "T01TEAM",
                "channel_id": "C01TEAM",
                "channel_type": "channel",
                "sender_slack_user_id": "U01HUMAN",
                "text": "<@U01BOT> sara look at this",
                "thread_ts": None,
                "event_ts": "1700000050.000001",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["handled"] is True
        assert body["install_id"] == slack_world["install"].id
        assert body["organization_id"] == slack_world["org"].id
        assert body["assistant_id"] == slack_world["sara"].agent_id
        assert body["bot_user_id"] == "U01BOT"
        assert body["thread_ts_for_route"] == "1700000050.000001"
        assert body["route_persisted"] is True
        assert body["routing_metadata"] == {
            "reason": "token_addressed",
            "token": "sara",
        }

    async def test_thread_route_upsert_and_prune(
        self,
        client: AsyncClient,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dbsession.commit()
        install_id = slack_world["install"].id
        alex_id = slack_world["alex"].agent_id

        upsert = await client.post(
            "/v0/admin/slack/thread-routes",
            json={
                "install_id": install_id,
                "channel_id": "C_RT",
                "thread_ts": "1700000030.000000",
                "assistant_id": alex_id,
                "ttl_days": 14,
            },
            headers=ADMIN_HEADERS,
        )
        assert upsert.status_code == status.HTTP_200_OK
        assert upsert.json()["assistant_id"] == alex_id

        # Manually expire the route, then prune.
        route = (
            dbsession.query(SlackThreadRoute)
            .filter(
                SlackThreadRoute.install_id == install_id,
                SlackThreadRoute.channel_id == "C_RT",
            )
            .one()
        )
        route.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        dbsession.commit()

        pruned = await client.post(
            "/v0/admin/slack/thread-routes/prune",
            headers=ADMIN_HEADERS,
        )
        assert pruned.status_code == status.HTTP_200_OK
        assert pruned.json()["deleted"] >= 1


# ============================================================================
# Slack app-level uninstall helper
# ============================================================================


class _FakeSlackResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSlackClient:
    """Stand-in for ``httpx.AsyncClient`` capturing one ``post`` call."""

    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeSlackClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object):
        self.calls.append({"url": url, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.mark.anyio
class TestSlackAppUninstall:
    """Response-branch coverage for ``uninstall_slack_app``."""

    async def test_ok_true_reports_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestra.services import slack_app

        fake = _FakeSlackClient(response=_FakeSlackResponse(200, {"ok": True}))
        monkeypatch.setattr(slack_app.httpx, "AsyncClient", lambda: fake)

        result = await slack_app.uninstall_slack_app(
            bot_token="xoxb-1",
            client_id="cid",
            client_secret="secret",
        )
        assert result is True
        assert fake.calls[0]["url"].endswith("/apps.uninstall")
        assert fake.calls[0]["params"] == {
            "client_id": "cid",
            "client_secret": "secret",
        }
        assert fake.calls[0]["headers"] == {"Authorization": "Bearer xoxb-1"}

    async def test_already_gone_error_is_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An already-uninstalled app must read as success (idempotent)."""
        from orchestra.services import slack_app

        fake = _FakeSlackClient(
            response=_FakeSlackResponse(200, {"ok": False, "error": "invalid_auth"}),
        )
        monkeypatch.setattr(slack_app.httpx, "AsyncClient", lambda: fake)

        result = await slack_app.uninstall_slack_app(
            bot_token="xoxb-1",
            client_id="cid",
            client_secret="secret",
        )
        assert result is True

    async def test_other_error_is_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestra.services import slack_app

        fake = _FakeSlackClient(
            response=_FakeSlackResponse(200, {"ok": False, "error": "ratelimited"}),
        )
        monkeypatch.setattr(slack_app.httpx, "AsyncClient", lambda: fake)

        result = await slack_app.uninstall_slack_app(
            bot_token="xoxb-1",
            client_id="cid",
            client_secret="secret",
        )
        assert result is False

    async def test_transport_error_is_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestra.services import slack_app

        fake = _FakeSlackClient(exc=RuntimeError("boom"))
        monkeypatch.setattr(slack_app.httpx, "AsyncClient", lambda: fake)

        result = await slack_app.uninstall_slack_app(
            bot_token="xoxb-1",
            client_id="cid",
            client_secret="secret",
        )
        assert result is False

    async def test_non_json_body_is_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orchestra.services import slack_app

        fake = _FakeSlackClient(
            response=_FakeSlackResponse(500, ValueError("not json")),
        )
        monkeypatch.setattr(slack_app.httpx, "AsyncClient", lambda: fake)

        result = await slack_app.uninstall_slack_app(
            bot_token="xoxb-1",
            client_id="cid",
            client_secret="secret",
        )
        assert result is False
