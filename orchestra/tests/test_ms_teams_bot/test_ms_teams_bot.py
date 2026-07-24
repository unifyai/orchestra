"""Tests for the MS Teams (Bot Framework) bot integration.

Covers:

* Install lifecycle in both deferred-ownership (Teams Store: pending →
  bind) and owner-known (Console: upsert) modes, plus the at-most-one-owner
  invariant and the active-tenant uniqueness that stops two owners holding
  the same Microsoft tenant live.
* Channel bindings and conversation routes (upsert idempotency, TTL
  refresh, expiry filtering, bulk prune) and the revoke cascade.
* The dispatcher routing tree — pending drop, personal 1:1 coordinator +
  established route, group/channel token addressing (unique / unknown /
  ambiguous), channel-binding fallback, mentioned fallback — plus the
  org two-pass sender-identity handshake.
* The admin HTTP surface end-to-end through the FastAPI client.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.ms_teams_bot_dao import MsTeamsBotDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    BillingAccount,
    MsTeamsBotConversationRoute,
    MsTeamsBotInstall,
    MsTeamsBotWelcome,
    Organization,
    User,
)
from orchestra.services.ms_teams_bot_dispatcher import resolve_inbound
from orchestra.tests.utils import ADMIN_HEADERS

# ============================================================================
# Fixtures / helpers
# ============================================================================


def _make_user(
    dbsession: Session,
    suffix: str,
    *,
    email: Optional[str] = None,
    name: str = "",
    last_name: str = "",
) -> User:
    ba = BillingAccount(credits=100)
    dbsession.add(ba)
    dbsession.flush()
    uid = f"teamsbot-user-{suffix}-{uuid.uuid4().hex[:8]}"
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        name=name or f"User {suffix}",
        last_name=last_name,
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_org(dbsession: Session, owner: User, suffix: str) -> Organization:
    org = Organization(owner_id=owner.id, name=f"Teams Org {suffix}")
    dbsession.add(org)
    dbsession.flush()
    return org


def _make_assistant(
    dbsession: Session,
    owner: User,
    *,
    first_name: str,
    surname: str = "Bot",
    organization: Optional[Organization] = None,
    is_coordinator: bool = False,
) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        organization_id=organization.id if organization is not None else None,
        first_name=first_name,
        surname=surname,
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
    tenant_id: str = "tenant-abc",
    pending: bool = False,
) -> MsTeamsBotInstall:
    install = MsTeamsBotInstall(
        organization_id=organization.id if organization is not None else None,
        user_id=user.id if user is not None else None,
        tenant_id=tenant_id,
        tenant_name="Contoso",
        bot_app_id="app-guid-001",
        service_url="https://smba.trafficmanager.net/amer/",
        bound_at=None if pending else datetime.now(timezone.utc),
    )
    dbsession.add(install)
    dbsession.flush()
    return install


# ============================================================================
# DAO — install lifecycle
# ============================================================================


class TestInstallDAO:
    def test_ensure_pending_install_creates_unbound_with_nonce(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        install, created = dao.ensure_pending_install(
            tenant_id="tenant-pending",
            bot_app_id="app-guid-001",
            tenant_name="Contoso",
            service_url="https://smba.example/",
        )
        assert created is True
        assert install.organization_id is None
        assert install.user_id is None
        assert install.bind_nonce
        assert install.bound_at is None

    def test_ensure_pending_install_is_idempotent_on_tenant(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        first, created_first = dao.ensure_pending_install(
            tenant_id="tenant-idem",
            bot_app_id="app-guid-001",
        )
        assert created_first is True
        nonce = first.bind_nonce
        second, created_second = dao.ensure_pending_install(
            tenant_id="tenant-idem",
            bot_app_id="app-guid-001",
            service_url="https://smba.updated/",
        )
        # A refresh of the existing tenant install is not a create.
        assert created_second is False
        assert second.id == first.id
        # Existing row refreshed in place; nonce/ownership untouched.
        assert second.bind_nonce == nonce
        assert second.service_url == "https://smba.updated/"

    def test_bind_install_consumes_nonce_and_sets_owner(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "bind")
        org = _make_org(dbsession, user, "bind")
        pending, _ = dao.ensure_pending_install(
            tenant_id="tenant-bind",
            bot_app_id="app-guid-001",
        )
        bound = dao.bind_install(pending.id, organization_id=org.id)
        assert bound.organization_id == org.id
        assert bound.user_id is None
        assert bound.bind_nonce is None
        assert bound.bound_at is not None

    def test_bind_install_requires_exactly_one_owner(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "bindxor")
        org = _make_org(dbsession, user, "bindxor")
        pending, _ = dao.ensure_pending_install(
            tenant_id="tenant-bindxor",
            bot_app_id="app-guid-001",
        )
        with pytest.raises(ValueError):
            dao.bind_install(pending.id, organization_id=org.id, user_id=user.id)
        with pytest.raises(ValueError):
            dao.bind_install(pending.id)

    def test_upsert_install_creates_then_refreshes(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "ups")
        org = _make_org(dbsession, user, "ups")
        created = dao.upsert_install(
            tenant_id="tenant-ups",
            bot_app_id="app-guid-001",
            organization_id=org.id,
            service_url="https://smba.a/",
        )
        assert created.bound_at is not None
        assert created.bind_nonce is None
        refreshed = dao.upsert_install(
            tenant_id="tenant-ups",
            bot_app_id="app-guid-001",
            organization_id=org.id,
            service_url="https://smba.b/",
        )
        assert refreshed.id == created.id
        assert refreshed.service_url == "https://smba.b/"

    def test_upsert_install_rejects_both_owners(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "both")
        org = _make_org(dbsession, user, "both")
        with pytest.raises(ValueError):
            dao.upsert_install(
                tenant_id="tenant-both",
                bot_app_id="app-guid-001",
                organization_id=org.id,
                user_id=user.id,
            )

    def test_active_tenant_uniqueness_across_owners(
        self,
        dbsession: Session,
    ) -> None:
        """At most one non-revoked install may exist per Microsoft tenant."""
        user_a = _make_user(dbsession, "ua")
        org_a = _make_org(dbsession, user_a, "a")
        user_b = _make_user(dbsession, "ub")
        org_b = _make_org(dbsession, user_b, "b")
        _make_install(dbsession, organization=org_a, tenant_id="tenant-dup")
        with pytest.raises(IntegrityError):
            _make_install(dbsession, organization=org_b, tenant_id="tenant-dup")
        dbsession.rollback()

    def test_reinstall_after_revoke_is_allowed(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "re")
        org = _make_org(dbsession, user, "re")
        first = _make_install(dbsession, organization=org, tenant_id="tenant-re")
        dao.revoke_install(first.id)
        # Per-owner uniqueness is permanent (only the active-tenant index is
        # revoked-aware), so re-installing the same (owner, tenant) reuses the
        # existing row and clears revoked_at to bring the install back live.
        second = dao.upsert_install(
            tenant_id="tenant-re",
            bot_app_id="app-guid-001",
            organization_id=org.id,
        )
        assert second.id == first.id
        assert second.revoked_at is None

    def test_bind_succeeds_despite_revoked_owner_leftover_user(
        self,
        dbsession: Session,
    ) -> None:
        """A revoked personal install must not block re-binding the same
        ``(user, tenant)``.

        The Teams Store path re-installs via ``ensure_pending_install`` (which
        skips revoked rows and so mints a *fresh* pending row) followed by
        ``bind_install``. Setting the owner on that fresh row used to collide
        with the revoked leftover under a ``user_id``-only unique index; the
        index is now scoped to active rows, so the bind is a clean insert.
        """
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "revbind")
        first = _make_install(dbsession, user=user, tenant_id="tenant-revbind")
        dao.revoke_install(first.id)

        pending, created = dao.ensure_pending_install(
            tenant_id="tenant-revbind",
            bot_app_id="app-guid-001",
        )
        # The revoked leftover is skipped, so this mints a fresh pending row.
        assert created is True
        assert pending.id != first.id

        bound = dao.bind_install(pending.id, user_id=user.id)
        assert bound.id == pending.id
        assert bound.user_id == user.id
        assert bound.revoked_at is None
        # The revoked leftover is retained (audit) beside the new active row.
        assert first.revoked_at is not None

    def test_bind_succeeds_despite_revoked_owner_leftover_org(
        self,
        dbsession: Session,
    ) -> None:
        """Org analogue of the personal revoked-leftover rebind."""
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "revbindorg")
        org = _make_org(dbsession, user, "revbindorg")
        first = _make_install(
            dbsession,
            organization=org,
            tenant_id="tenant-revbindorg",
        )
        dao.revoke_install(first.id)

        pending, created = dao.ensure_pending_install(
            tenant_id="tenant-revbindorg",
            bot_app_id="app-guid-001",
        )
        assert created is True
        assert pending.id != first.id

        bound = dao.bind_install(pending.id, organization_id=org.id)
        assert bound.id == pending.id
        assert bound.organization_id == org.id
        assert bound.revoked_at is None
        assert first.revoked_at is not None

    def test_two_active_installs_same_user_tenant_rejected(
        self,
        dbsession: Session,
    ) -> None:
        """Scoping the owner-tenant index to active rows must not weaken the
        guarantee that one ``(user, tenant)`` holds at most one *active*
        install."""
        user = _make_user(dbsession, "dupuser")
        _make_install(dbsession, user=user, tenant_id="tenant-dupuser")
        with pytest.raises(IntegrityError):
            _make_install(dbsession, user=user, tenant_id="tenant-dupuser")
        dbsession.rollback()

    def test_ensure_pending_install_recovers_from_concurrent_winner(
        self,
        dbsession: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent add-event that wins the active-tenant slot must not 500.

        Reproduces the Teams Store race: ``conversationUpdate`` and
        ``installationUpdate`` for the same tenant both reach
        ``ensure_pending_install`` at once. The loser's INSERT collides on
        ``ux_ms_teams_bot_install_active_tenant``; that collision must stay
        contained by the savepoint (leaving the *outer* transaction alive) so
        the loser recovers by returning the committed winner with
        ``created=False`` — never propagating as a 500 that would strand the
        adapter's welcome DM. Simulated by making the first
        ``get_install_by_tenant`` lookup miss the already-present active row.
        """
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "race")
        org = _make_org(dbsession, user, "race")
        winner = _make_install(dbsession, organization=org, tenant_id="tenant-race")

        real_lookup = dao.get_install_by_tenant
        calls = {"n": 0}

        def _miss_first(tenant_id: str) -> Optional[MsTeamsBotInstall]:
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_lookup(tenant_id)

        monkeypatch.setattr(dao, "get_install_by_tenant", _miss_first)

        install, created = dao.ensure_pending_install(
            tenant_id="tenant-race",
            bot_app_id="app-guid-001",
        )

        assert created is False
        assert install.id == winner.id
        # The outer transaction survived the contained collision: a follow-up
        # query must succeed (a poisoned transaction would raise here).
        assert dao.list_installs()

    def test_get_install_by_tenant_ignores_revoked(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "rev")
        org = _make_org(dbsession, user, "rev")
        install = _make_install(dbsession, organization=org, tenant_id="tenant-rev")
        dao.revoke_install(install.id)
        assert dao.get_install_by_tenant("tenant-rev") is None

    def test_get_install_by_nonce(self, dbsession: Session) -> None:
        dao = MsTeamsBotDAO(dbsession)
        pending, _ = dao.ensure_pending_install(
            tenant_id="tenant-nonce",
            bot_app_id="app-guid-001",
        )
        found = dao.get_install_by_nonce(pending.bind_nonce)
        assert found is not None and found.id == pending.id

    def test_revoke_drops_bindings_and_routes(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "drop")
        org = _make_org(dbsession, user, "drop")
        assistant = _make_assistant(
            dbsession,
            user,
            first_name="Ada",
            organization=org,
        )
        install = _make_install(dbsession, organization=org, tenant_id="tenant-drop")
        dao.bind_channel(install.id, "19:chan@thread.tacv2", assistant.agent_id)
        dao.upsert_conversation_route(install.id, "conv-1", assistant.agent_id)
        dao.claim_welcome(install.id, "conv-1")
        dao.revoke_install(install.id)
        assert dao.list_channel_bindings(install.id) == []
        assert dao.get_conversation_route(install.id, "conv-1") is None
        # Welcome claims are dropped on revoke so a genuine reinstall greets
        # each conversation afresh rather than staying silent.
        remaining_welcomes = (
            dbsession.query(MsTeamsBotWelcome)
            .filter(MsTeamsBotWelcome.install_id == install.id)
            .count()
        )
        assert remaining_welcomes == 0

    def test_claim_welcome_is_one_shot_per_conversation(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "welcome")
        org = _make_org(dbsession, user, "welcome")
        install = _make_install(
            dbsession,
            organization=org,
            tenant_id="tenant-welcome",
        )
        # First delivery for the conversation wins the claim.
        assert dao.claim_welcome(install.id, "conv-personal") is True
        # A redelivered bot-add for the same conversation stays silent.
        assert dao.claim_welcome(install.id, "conv-personal") is False
        # A distinct conversation (e.g. a team channel) still greets.
        assert dao.claim_welcome(install.id, "conv-channel") is True


# ============================================================================
# DAO — channel bindings + conversation routes
# ============================================================================


class TestRoutingStateDAO:
    def test_bind_channel_idempotent_reassign(self, dbsession: Session) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "bc")
        org = _make_org(dbsession, user, "bc")
        a1 = _make_assistant(dbsession, user, first_name="One", organization=org)
        a2 = _make_assistant(dbsession, user, first_name="Two", organization=org)
        install = _make_install(dbsession, organization=org, tenant_id="tenant-bc")
        first = dao.bind_channel(install.id, "19:c@thread.tacv2", a1.agent_id)
        again = dao.bind_channel(install.id, "19:c@thread.tacv2", a2.agent_id)
        assert again.id == first.id
        assert again.assistant_id == a2.agent_id

    def test_conversation_route_upsert_refreshes_ttl(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "rt")
        org = _make_org(dbsession, user, "rt")
        assistant = _make_assistant(dbsession, user, first_name="Rex", organization=org)
        install = _make_install(dbsession, organization=org, tenant_id="tenant-rt")
        route = dao.upsert_conversation_route(
            install.id,
            "conv-x",
            assistant.agent_id,
            conversation_reference='{"a": 1}',
        )
        first_expiry = route.expires_at
        route.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        dbsession.flush()
        again = dao.upsert_conversation_route(
            install.id,
            "conv-x",
            assistant.agent_id,
            conversation_reference='{"a": 2}',
        )
        assert again.id == route.id
        assert again.conversation_reference == '{"a": 2}'
        assert again.expires_at > first_expiry - timedelta(days=1)

    def test_get_conversation_route_filters_expired(
        self,
        dbsession: Session,
    ) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "exp")
        org = _make_org(dbsession, user, "exp")
        assistant = _make_assistant(dbsession, user, first_name="Eve", organization=org)
        install = _make_install(dbsession, organization=org, tenant_id="tenant-exp")
        route = dao.upsert_conversation_route(install.id, "conv-e", assistant.agent_id)
        route.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        dbsession.flush()
        assert dao.get_conversation_route(install.id, "conv-e") is None

    def test_delete_expired_routes_prunes(self, dbsession: Session) -> None:
        dao = MsTeamsBotDAO(dbsession)
        user = _make_user(dbsession, "prune")
        org = _make_org(dbsession, user, "prune")
        assistant = _make_assistant(dbsession, user, first_name="Pia", organization=org)
        install = _make_install(dbsession, organization=org, tenant_id="tenant-prune")
        live = dao.upsert_conversation_route(
            install.id,
            "conv-live",
            assistant.agent_id,
        )
        dead = dao.upsert_conversation_route(
            install.id,
            "conv-dead",
            assistant.agent_id,
        )
        dead.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        dbsession.flush()
        deleted = dao.delete_expired_routes()
        assert deleted == 1
        remaining = dbsession.query(MsTeamsBotConversationRoute).all()
        assert [r.id for r in remaining] == [live.id]


# ============================================================================
# Dispatcher
# ============================================================================


class TestDispatcher:
    def test_no_install_returns_none(self, dbsession: Session) -> None:
        assert (
            resolve_inbound(
                dbsession,
                tenant_id="tenant-missing",
                conversation_id="c1",
                conversation_type="personal",
                channel_id=None,
                sender_aad_object_id="aad-1",
                bot_mentioned=False,
                addressed_text="hi",
            )
            is None
        )

    def test_pending_install_is_dropped(self, dbsession: Session) -> None:
        _make_install(dbsession, pending=True, tenant_id="tenant-pend", user=None)
        assert (
            resolve_inbound(
                dbsession,
                tenant_id="tenant-pend",
                conversation_id="c1",
                conversation_type="personal",
                channel_id=None,
                sender_aad_object_id="aad-1",
                bot_mentioned=False,
                addressed_text="hi",
            )
            is None
        )

    def test_personal_chat_routes_to_coordinator_and_persists(
        self,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "pers")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            is_coordinator=True,
        )
        install = _make_install(dbsession, user=user, tenant_id="tenant-pers")
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-pers",
            conversation_id="conv-p",
            conversation_type="personal",
            channel_id=None,
            sender_aad_object_id="aad-1",
            bot_mentioned=False,
            addressed_text="hello",
            conversation_reference='{"ref": 1}',
        )
        assert result is not None
        assert result.assistant_id == coord.agent_id
        assert result.routing_metadata["reason"] == "initial_chat"
        assert result.route_persisted is True
        # Personal install: the lone human in a 1:1 is the account owner, so
        # the inbound is boss-authored (attributed to the boss contact, not a
        # freshly minted per-name contact).
        assert result.sender_is_owner is True
        dao = MsTeamsBotDAO(dbsession)
        assert dao.get_conversation_route(install.id, "conv-p") is not None

    def test_personal_established_route_wins(self, dbsession: Session) -> None:
        user = _make_user(dbsession, "estab")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            is_coordinator=True,
        )
        other = _make_assistant(dbsession, user, first_name="Other")
        install = _make_install(dbsession, user=user, tenant_id="tenant-estab")
        dao = MsTeamsBotDAO(dbsession)
        dao.upsert_conversation_route(install.id, "conv-e", other.agent_id)
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-estab",
            conversation_id="conv-e",
            conversation_type="personal",
            channel_id=None,
            sender_aad_object_id="aad-1",
            bot_mentioned=False,
            addressed_text="hello again",
        )
        assert result is not None
        assert result.assistant_id == other.agent_id
        assert result.assistant_id != coord.agent_id
        # Owner attribution holds on the established-route path too.
        assert result.sender_is_owner is True

    def test_group_token_addressed_unique(self, dbsession: Session) -> None:
        user = _make_user(dbsession, "tok")
        org = _make_org(dbsession, user, "tok")
        _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        lily = _make_assistant(dbsession, user, first_name="Lily", organization=org)
        _make_install(dbsession, organization=org, tenant_id="tenant-tok")
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-tok",
            conversation_id="conv-g",
            conversation_type="groupChat",
            channel_id=None,
            sender_aad_object_id="aad-1",
            bot_mentioned=True,
            addressed_text="Lily can you help",
        )
        assert result is not None
        assert result.assistant_id == lily.agent_id
        assert result.routing_metadata["reason"] == "token_addressed"
        assert result.routing_metadata["token"] == "Lily"

    def test_group_unknown_token_falls_back_to_coordinator(
        self,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "unk")
        org = _make_org(dbsession, user, "unk")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        _make_install(dbsession, organization=org, tenant_id="tenant-unk")
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-unk",
            conversation_id="conv-u",
            conversation_type="channel",
            channel_id="19:c@thread.tacv2",
            sender_aad_object_id="aad-1",
            bot_mentioned=True,
            addressed_text="Nobody here",
            sender_identity_provided=True,
        )
        assert result is not None
        assert result.assistant_id == coord.agent_id
        assert result.routing_metadata["reason"] == "unknown_token"

    def test_group_ambiguous_token_surfaces_candidates(
        self,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "amb")
        org = _make_org(dbsession, user, "amb")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        _make_assistant(
            dbsession,
            user,
            first_name="Alex",
            surname="One",
            organization=org,
        )
        _make_assistant(
            dbsession,
            user,
            first_name="Alex",
            surname="Two",
            organization=org,
        )
        _make_install(dbsession, organization=org, tenant_id="tenant-amb")
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-amb",
            conversation_id="conv-a",
            conversation_type="groupChat",
            channel_id=None,
            sender_aad_object_id="aad-1",
            bot_mentioned=True,
            addressed_text="Alex please",
            sender_identity_provided=True,
        )
        assert result is not None
        assert result.assistant_id == coord.agent_id
        assert result.routing_metadata["reason"] == "ambiguous_token"
        assert len(result.routing_metadata["candidates"]) == 2

    def test_channel_binding_fallback(self, dbsession: Session) -> None:
        user = _make_user(dbsession, "bind")
        org = _make_org(dbsession, user, "bind")
        _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        bound = _make_assistant(dbsession, user, first_name="Bound", organization=org)
        install = _make_install(dbsession, organization=org, tenant_id="tenant-cb")
        dao = MsTeamsBotDAO(dbsession)
        dao.bind_channel(install.id, "19:c@thread.tacv2", bound.agent_id)
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-cb",
            conversation_id="conv-cb",
            conversation_type="channel",
            channel_id="19:c@thread.tacv2",
            sender_aad_object_id="aad-1",
            bot_mentioned=True,
            addressed_text="",
        )
        assert result is not None
        assert result.assistant_id == bound.agent_id
        assert result.routing_metadata["reason"] == "channel_binding"

    def test_mentioned_fallback_to_coordinator(self, dbsession: Session) -> None:
        user = _make_user(dbsession, "mf")
        org = _make_org(dbsession, user, "mf")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            organization=org,
            is_coordinator=True,
        )
        _make_install(dbsession, organization=org, tenant_id="tenant-mf")
        result = resolve_inbound(
            dbsession,
            tenant_id="tenant-mf",
            conversation_id="conv-mf",
            conversation_type="channel",
            channel_id="19:c@thread.tacv2",
            sender_aad_object_id="aad-1",
            bot_mentioned=True,
            addressed_text="",
            sender_identity_provided=True,
        )
        assert result is not None
        assert result.assistant_id == coord.agent_id
        assert result.routing_metadata["reason"] == "mentioned_fallback"

    def test_org_two_pass_sender_identity_handshake(
        self,
        dbsession: Session,
    ) -> None:
        """First pass provisions org coordinator + asks for identity; second
        pass with the sender's email pins to that member's own coordinator."""
        owner1 = _make_user(dbsession, "m1", email="m1@corp.test")
        org = _make_org(dbsession, owner1, "h")
        coord1 = _make_assistant(
            dbsession,
            owner1,
            first_name="CoordOne",
            organization=org,
            is_coordinator=True,
        )
        owner2 = _make_user(dbsession, "m2", email="m2@corp.test")
        coord2 = _make_assistant(
            dbsession,
            owner2,
            first_name="CoordTwo",
            organization=org,
            is_coordinator=True,
        )
        _make_install(dbsession, organization=org, tenant_id="tenant-h")

        first = resolve_inbound(
            dbsession,
            tenant_id="tenant-h",
            conversation_id="conv-h",
            conversation_type="personal",
            channel_id=None,
            sender_aad_object_id="aad-2",
            bot_mentioned=False,
            addressed_text="hello",
            sender_identity_provided=False,
        )
        assert first is not None
        # Deterministic org coordinator (lowest agent_id) provisionally.
        assert first.assistant_id == coord1.agent_id
        assert first.needs_sender_identity is True
        assert first.route_persisted is False
        # Identity not yet resolved, so we can't claim the provisional
        # coordinator's owner is the sender.
        assert first.sender_is_owner is False

        second = resolve_inbound(
            dbsession,
            tenant_id="tenant-h",
            conversation_id="conv-h",
            conversation_type="personal",
            channel_id=None,
            sender_aad_object_id="aad-2",
            bot_mentioned=False,
            addressed_text="hello",
            sender_email="m2@corp.test",
            sender_identity_provided=True,
        )
        assert second is not None
        assert second.assistant_id == coord2.agent_id
        assert second.needs_sender_identity is False
        # The sender maps to owner2, who owns coord2 — so relative to the
        # resolved workspace assistant the sender is the owner (boss).
        assert second.sender_is_owner is True


# ============================================================================
# Admin HTTP surface
# ============================================================================


class TestAdminEndpoints:
    async def test_pending_install_then_bind(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-bind")
        org = _make_org(dbsession, user, "http")
        dbsession.commit()

        pending = await client.post(
            "/v0/admin/ms-teams-bot/pending-install",
            json={"tenant_id": "tenant-http", "bot_app_id": "app-guid-001"},
            headers=ADMIN_HEADERS,
        )
        assert pending.status_code == status.HTTP_200_OK
        body = pending.json()
        assert body["pending"] is True
        assert body["bind_nonce"]
        # First call inserted the row, so the adapter should welcome once.
        assert body["created"] is True
        assert body["connect_url"] and body["bind_nonce"] in body["connect_url"]

        # A repeat add-event refreshes the same row and must not re-welcome.
        again = await client.post(
            "/v0/admin/ms-teams-bot/pending-install",
            json={"tenant_id": "tenant-http", "bot_app_id": "app-guid-001"},
            headers=ADMIN_HEADERS,
        )
        assert again.status_code == status.HTTP_200_OK
        assert again.json()["created"] is False
        assert again.json()["id"] == body["id"]

        bound = await client.post(
            "/v0/admin/ms-teams-bot/bind",
            json={"install_id": body["id"], "organization_id": org.id},
            headers=ADMIN_HEADERS,
        )
        assert bound.status_code == status.HTTP_200_OK
        bound_body = bound.json()
        assert bound_body["organization_id"] == org.id
        assert bound_body["pending"] is False

    async def test_get_install_by_nonce(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        dbsession.commit()
        pending = await client.post(
            "/v0/admin/ms-teams-bot/pending-install",
            json={"tenant_id": "tenant-nonce-http", "bot_app_id": "app-guid-001"},
            headers=ADMIN_HEADERS,
        )
        nonce = pending.json()["bind_nonce"]
        found = await client.get(
            "/v0/admin/ms-teams-bot/install",
            params={"bind_nonce": nonce, "include_nonce": True},
            headers=ADMIN_HEADERS,
        )
        assert found.status_code == status.HTTP_200_OK
        assert found.json()["id"] == pending.json()["id"]

    async def test_dispatch_handled_false_when_no_install(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/v0/admin/ms-teams-bot/dispatch",
            json={
                "tenant_id": "tenant-none",
                "conversation_id": "c1",
                "conversation_type": "personal",
                "sender_aad_object_id": "aad-1",
                "bot_mentioned": False,
                "addressed_text": "hi",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["handled"] is False
        assert body["install_state"] == "none"
        assert body["connect_url"] is None

    async def test_dispatch_pending_install_returns_connect_url(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        """A message on a still-pending install must surface the connect link
        so the adapter can reply with it (Store-review path: reviewer DMs the
        bot before binding)."""
        dbsession.commit()
        pending = await client.post(
            "/v0/admin/ms-teams-bot/pending-install",
            json={"tenant_id": "tenant-pending-disp", "bot_app_id": "app-guid-001"},
            headers=ADMIN_HEADERS,
        )
        nonce = pending.json()["bind_nonce"]

        resp = await client.post(
            "/v0/admin/ms-teams-bot/dispatch",
            json={
                "tenant_id": "tenant-pending-disp",
                "conversation_id": "conv-pending",
                "conversation_type": "personal",
                "sender_aad_object_id": "aad-1",
                "bot_mentioned": False,
                "addressed_text": "hi",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["handled"] is False
        assert body["install_state"] == "pending"
        assert body["install_id"] == pending.json()["id"]
        assert body["bot_app_id"] == "app-guid-001"
        assert body["connect_url"] and nonce in body["connect_url"]

    async def test_dispatch_personal_happy_path(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-disp")
        coord = _make_assistant(
            dbsession,
            user,
            first_name="Coord",
            is_coordinator=True,
        )
        _make_install(dbsession, user=user, tenant_id="tenant-http-disp")
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/ms-teams-bot/dispatch",
            json={
                "tenant_id": "tenant-http-disp",
                "conversation_id": "conv-http",
                "conversation_type": "personal",
                "sender_aad_object_id": "aad-1",
                "bot_mentioned": False,
                "addressed_text": "hey",
                "conversation_reference": '{"ref": 1}',
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["handled"] is True
        assert body["install_state"] == "bound"
        assert body["assistant_id"] == coord.agent_id
        assert body["routing_metadata"]["reason"] == "initial_chat"

    async def test_dispatch_routing_fault_degrades_to_handled_false(
        self,
        client: AsyncClient,
        dbsession: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = _make_user(dbsession, "http-fault")
        _make_assistant(dbsession, user, first_name="Coord", is_coordinator=True)
        _make_install(dbsession, user=user, tenant_id="tenant-fault")
        dbsession.commit()

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated routing fault")

        monkeypatch.setattr(
            "orchestra.web.api.ms_teams_bot.views.resolve_inbound",
            _boom,
        )
        resp = await client.post(
            "/v0/admin/ms-teams-bot/dispatch",
            json={
                "tenant_id": "tenant-fault",
                "conversation_id": "conv-fault",
                "conversation_type": "personal",
                "sender_aad_object_id": "aad-1",
                "bot_mentioned": False,
                "addressed_text": "hi",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["handled"] is False

    async def test_channel_binding_lifecycle(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-cb")
        org = _make_org(dbsession, user, "http-cb")
        assistant = _make_assistant(
            dbsession,
            user,
            first_name="Ada",
            organization=org,
        )
        install = _make_install(dbsession, organization=org, tenant_id="tenant-http-cb")
        dbsession.commit()

        create = await client.post(
            "/v0/admin/ms-teams-bot/channel-bindings",
            json={
                "install_id": install.id,
                "channel_id": "19:c@thread.tacv2",
                "assistant_id": assistant.agent_id,
                "channel_name": "design",
            },
            headers=ADMIN_HEADERS,
        )
        assert create.status_code == status.HTTP_200_OK
        assert create.json()["assistant_id"] == assistant.agent_id

        listed = await client.get(
            "/v0/admin/ms-teams-bot/channel-bindings",
            params={"install_id": install.id},
            headers=ADMIN_HEADERS,
        )
        assert listed.status_code == status.HTTP_200_OK
        assert any(r["channel_id"] == "19:c@thread.tacv2" for r in listed.json())

        deleted = await client.delete(
            "/v0/admin/ms-teams-bot/channel-bindings",
            params={"install_id": install.id, "channel_id": "19:c@thread.tacv2"},
            headers=ADMIN_HEADERS,
        )
        assert deleted.status_code == status.HTTP_200_OK
        assert deleted.json() == {"deleted": True}

    async def test_conversation_route_upsert_get_prune(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-rt")
        org = _make_org(dbsession, user, "http-rt")
        assistant = _make_assistant(
            dbsession,
            user,
            first_name="Rex",
            organization=org,
        )
        install = _make_install(dbsession, organization=org, tenant_id="tenant-http-rt")
        dbsession.commit()

        upsert = await client.post(
            "/v0/admin/ms-teams-bot/conversation-routes",
            json={
                "install_id": install.id,
                "conversation_id": "conv-http-rt",
                "assistant_id": assistant.agent_id,
                "conversation_reference": '{"ref": 1}',
            },
            headers=ADMIN_HEADERS,
        )
        assert upsert.status_code == status.HTTP_200_OK

        got = await client.get(
            "/v0/admin/ms-teams-bot/conversation-routes",
            params={"install_id": install.id, "conversation_id": "conv-http-rt"},
            headers=ADMIN_HEADERS,
        )
        assert got.status_code == status.HTTP_200_OK
        assert got.json()["assistant_id"] == assistant.agent_id

        pruned = await client.post(
            "/v0/admin/ms-teams-bot/conversation-routes/prune",
            headers=ADMIN_HEADERS,
        )
        assert pruned.status_code == status.HTTP_200_OK
        assert "deleted" in pruned.json()

    async def test_revoke_install(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-rev")
        org = _make_org(dbsession, user, "http-rev")
        install = _make_install(
            dbsession,
            organization=org,
            tenant_id="tenant-http-rev",
        )
        dbsession.commit()
        resp = await client.delete(
            f"/v0/admin/ms-teams-bot/install/{install.id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json() == {"id": install.id, "revoked": True}

    async def test_welcome_claim_idempotent(
        self,
        client: AsyncClient,
        dbsession: Session,
    ) -> None:
        user = _make_user(dbsession, "http-welcome")
        org = _make_org(dbsession, user, "http-welcome")
        install = _make_install(
            dbsession,
            organization=org,
            tenant_id="tenant-http-welcome",
        )
        dbsession.commit()

        first = await client.post(
            "/v0/admin/ms-teams-bot/welcome-claim",
            json={"install_id": install.id, "conversation_id": "conv-http-welcome"},
            headers=ADMIN_HEADERS,
        )
        assert first.status_code == status.HTTP_200_OK
        assert first.json() == {"claimed": True}

        # A redelivered bot-add for the same conversation must not re-welcome.
        again = await client.post(
            "/v0/admin/ms-teams-bot/welcome-claim",
            json={"install_id": install.id, "conversation_id": "conv-http-welcome"},
            headers=ADMIN_HEADERS,
        )
        assert again.status_code == status.HTTP_200_OK
        assert again.json() == {"claimed": False}
