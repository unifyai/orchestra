"""Tests for the Slack integration: schema, DAO, dispatcher, admin endpoints.

Covers:

* Install/binding/route schema constraints and lifecycle (DAO).
* The full dispatcher routing tree — DM vs channel, token addressing
  (unique / unknown / ambiguous), thread inheritance, channel binding
  fallback, bot-echo suppression.
* ``AssistantDAO`` additions (``coordinator_for_org``, ``resolve_token``).
* The admin HTTP surface end-to-end through the FastAPI client.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.models.orchestra_models import (
    DM_ROOT_SENTINEL,
    Assistant,
    BillingAccount,
    Organization,
    SlackChannelBinding,
    SlackInstall,
    SlackThreadRoute,
    User,
)
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
    organization: Organization,
    is_coordinator: bool = False,
) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        organization_id=organization.id,
        first_name=first_name,
        surname="Bot",
        is_coordinator=is_coordinator,
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_install(
    dbsession: Session,
    organization: Organization,
    *,
    slack_team_id: str = "T01TEAM",
    bot_user_id: str = "U01BOT",
    bot_access_token: str = "xoxb-test",
) -> SlackInstall:
    install = SlackInstall(
        organization_id=organization.id,
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


@pytest.fixture
def slack_world(dbsession: Session):
    """Minimal world: org with a Coordinator + named assistants + Slack install."""
    owner = _make_user(dbsession, "owner")
    org = _make_org(dbsession, owner, "slack")
    coordinator = _make_assistant(
        dbsession,
        owner,
        first_name="Coordinator",
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
    install = _make_install(dbsession, org)
    return {
        "owner": owner,
        "org": org,
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

    def test_install_unique_per_org_team(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        org = slack_world["org"]
        duplicate = SlackInstall(
            organization_id=org.id,
            slack_team_id="T01TEAM",
            slack_app_id="A01APP",
            bot_user_id="U01BOT2",
            bot_access_token="xoxb-dup",
        )
        dbsession.add(duplicate)
        with pytest.raises(IntegrityError, match="uq_slack_install_org_team"):
            dbsession.flush()

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
        coordinator = AssistantDAO(dbsession).coordinator_for_org(
            slack_world["org"].id,
        )
        assert coordinator is not None
        assert coordinator.agent_id == slack_world["coordinator"].agent_id

    def test_coordinator_for_org_returns_none_when_none_exists(
        self,
        dbsession: Session,
    ) -> None:
        owner = _make_user(dbsession, "no-coord")
        org = _make_org(dbsession, owner, "no-coord")
        _make_assistant(dbsession, owner, first_name="Solo", organization=org)
        assert AssistantDAO(dbsession).coordinator_for_org(org.id) is None

    def test_resolve_token_unique_case_insensitive(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        dao = AssistantDAO(dbsession)
        for token in ("Alex", "alex", "ALEX", "  alex  "):
            matches = dao.resolve_token(slack_world["org"].id, token)
            assert len(matches) == 1
            assert matches[0].agent_id == slack_world["alex"].agent_id

    def test_resolve_token_unknown_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        matches = AssistantDAO(dbsession).resolve_token(
            slack_world["org"].id,
            "nonexistent",
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

        matches = AssistantDAO(dbsession).resolve_token(org.id, "Sara")
        assert len(matches) == 2

    def test_resolve_token_empty_string_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        assert AssistantDAO(dbsession).resolve_token(slack_world["org"].id, "") == []

    def test_resolve_token_none_returns_empty(
        self,
        dbsession: Session,
        slack_world: dict,
    ) -> None:
        """Defensive: callers may pass through a missing-token sentinel."""
        assert (
            AssistantDAO(dbsession).resolve_token(slack_world["org"].id, None)  # type: ignore[arg-type]
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

        matches_here = AssistantDAO(dbsession).resolve_token(
            slack_world["org"].id,
            "Alex",
        )
        assert len(matches_here) == 1
        assert matches_here[0].agent_id == slack_world["alex"].agent_id

        matches_there = AssistantDAO(dbsession).resolve_token(other_org.id, "Alex")
        assert len(matches_there) == 1
        assert matches_there[0].organization_id == other_org.id


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
        # Coordinator gets the thread so it can clarify in place.
        assert resolution.route_persisted is True
        assert resolution.thread_ts_for_route == "1700000003.000000"

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

    async def test_install_get_requires_one_of_team_or_org(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.get("/v0/admin/slack/install", headers=ADMIN_HEADERS)
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
        assert resp.json() == {"id": install_id, "revoked": True}

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
            "assistant_id": None,
            "bot_user_id": None,
            "thread_ts_for_route": None,
            "route_persisted": False,
            "routing_metadata": {},
        }

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
