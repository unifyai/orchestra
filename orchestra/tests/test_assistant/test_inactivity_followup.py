"""Tests for the per-user inactivity follow-up routine + Coordinator emails.

The follow-up is Orchestra-templated: quiet users' personal Coordinators
are selected, then a soft check-in is emailed from the shared twin@
mailbox. These tests assert on the email send helper and
``last_followup_sent_at`` bookkeeping — not on GKE / adapter wakes.

Covers:
    1. AssistantDAO helpers
        - touch_last_correspondence_at updates and clears followup
        - mark_followup_sent
        - set_inactivity_followup_opt_out
        - find_followup_candidates per-user activity aggregation, re-arm
          logic, opt-out / demo / local exclusion, coordinator scoping
    2. inactivity follow-up routine
        - no-op when no candidates
        - sends templated email + stamps last_followup_sent_at
        - fails cleanly (no stamp) when the send returns False / raises
        - skips opted-out coordinators
        - never deletes or deprovisions anything
    3. Admin endpoints
        - touch-activity, opt-out-followups, opt-in-followups (200 + 404)
        - inactivity-followup trigger endpoint returns followup metrics
    4. Welcome + follow-up email templates + send helpers
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import Assistant, DemoAssistantMeta, User
from orchestra.routines.inactivity_followup import (
    InactivityFollowupResult,
    run_inactivity_followup,
)

# Patch target for the templated email send the routine performs per owner.
_SEND_TARGET = (
    "orchestra.routines.inactivity_notifications."
    "send_coordinator_inactivity_followup_email"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(
    dbsession: Session,
    uid: str,
    *,
    email: str = "",
    first_name: str | None = None,
) -> User:
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        name=first_name,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(
    dbsession: Session,
    user_id: str,
    *,
    first_name: str = "InactBot",
    last_correspondence_at: datetime | None = None,
    last_followup_sent_at: datetime | None = None,
    inactivity_followup_opted_out: bool = False,
    is_coordinator: bool = False,
    organization_id: int | None = None,
    demo_id: int | None = None,
    is_local: bool = False,
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        last_correspondence_at=last_correspondence_at,
        last_followup_sent_at=last_followup_sent_at,
        inactivity_followup_opted_out=inactivity_followup_opted_out,
        is_coordinator=is_coordinator,
        organization_id=organization_id,
        demo_id=demo_id,
        is_local=is_local,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def _make_coordinator(
    dbsession: Session,
    user_id: str,
    *,
    last_correspondence_at: datetime | None = None,
    last_followup_sent_at: datetime | None = None,
    inactivity_followup_opted_out: bool = False,
    is_local: bool = False,
    demo_id: int | None = None,
) -> Assistant:
    return _make_assistant(
        dbsession,
        user_id,
        first_name="T-W1N",
        last_correspondence_at=last_correspondence_at,
        last_followup_sent_at=last_followup_sent_at,
        inactivity_followup_opted_out=inactivity_followup_opted_out,
        is_coordinator=True,
        organization_id=None,
        is_local=is_local,
        demo_id=demo_id,
    )


@pytest.fixture(autouse=True)
def zero_jitter():
    """Disable jitter so tests run instantly."""
    from orchestra.settings import settings

    original = settings.inactivity_followup_jitter_seconds
    settings.inactivity_followup_jitter_seconds = 0
    yield
    settings.inactivity_followup_jitter_seconds = original


@pytest.fixture
def mock_send():
    """Replace the templated email send with an AsyncMock (succeeds by default)."""
    with patch(_SEND_TARGET, new_callable=AsyncMock) as mock:
        mock.return_value = True
        yield mock


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ===========================================================================
# 1. AssistantDAO helpers
# ===========================================================================


class TestDAOTouchAndMark:
    def test_touch_updates_correspondence_and_clears_followup(
        self,
        dbsession: Session,
    ):
        user = _make_user(dbsession, "dao_u1")
        long_ago = _cutoff(10)
        a = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=long_ago,
            last_followup_sent_at=long_ago,
        )

        now = datetime.now(timezone.utc)
        dao = AssistantDAO(dbsession)
        rows = dao.touch_last_correspondence_at(a.agent_id, now)
        dbsession.flush()
        dbsession.refresh(a)

        assert rows == 1
        assert a.last_correspondence_at == now
        assert a.last_followup_sent_at is None

    def test_touch_on_unknown_assistant_returns_zero(self, dbsession: Session):
        dao = AssistantDAO(dbsession)
        assert (
            dao.touch_last_correspondence_at(999_999, datetime.now(timezone.utc)) == 0
        )

    def test_mark_followup_sent_sets_timestamp(self, dbsession: Session):
        user = _make_user(dbsession, "dao_u2")
        a = _make_assistant(dbsession, user.id)

        now = datetime.now(timezone.utc)
        dao = AssistantDAO(dbsession)
        dao.mark_followup_sent(a.agent_id, now)
        dbsession.flush()
        dbsession.refresh(a)

        assert a.last_followup_sent_at == now

    def test_set_opt_out_toggles_flag(self, dbsession: Session):
        user = _make_user(dbsession, "dao_u3")
        a = _make_coordinator(dbsession, user.id)
        assert a.inactivity_followup_opted_out is False

        dao = AssistantDAO(dbsession)
        rows = dao.set_inactivity_followup_opt_out(a.agent_id, True)
        dbsession.flush()
        dbsession.refresh(a)
        assert rows == 1
        assert a.inactivity_followup_opted_out is True

        dao.set_inactivity_followup_opt_out(a.agent_id, False)
        dbsession.flush()
        dbsession.refresh(a)
        assert a.inactivity_followup_opted_out is False

    def test_set_opt_out_unknown_assistant_returns_zero(self, dbsession: Session):
        dao = AssistantDAO(dbsession)
        assert dao.set_inactivity_followup_opt_out(999_999, True) == 0


class TestDAOFindFollowupCandidates:
    def test_quiet_user_is_a_candidate(self, dbsession: Session):
        user = _make_user(dbsession, "fup_u1")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id in ids

    def test_active_user_excluded(self, dbsession: Session):
        user = _make_user(dbsession, "fup_u2")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(1),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_opted_out_user_excluded(self, dbsession: Session):
        user = _make_user(dbsession, "fup_optout")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            inactivity_followup_opted_out=True,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_activity_aggregates_across_all_assistants(self, dbsession: Session):
        """Recent activity on ANY assistant keeps the whole user active."""
        user = _make_user(dbsession, "fup_u3")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(20),
        )
        # A specialist the user spoke to an hour ago.
        _make_assistant(
            dbsession,
            user.id,
            first_name="Specialist",
            last_correspondence_at=_cutoff(0),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_quiet_across_all_assistants_is_candidate(self, dbsession: Session):
        """Every assistant quiet (coordinator + specialist) => follow up."""
        user = _make_user(dbsession, "fup_u4")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(20),
        )
        _make_assistant(
            dbsession,
            user.id,
            first_name="Specialist",
            last_correspondence_at=_cutoff(9),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id in ids

    def test_never_engaged_user_followed_up_after_window(self, dbsession: Session):
        """A user whose only baseline is signup-time (last_correspondence_at
        defaults to now() at creation) is followed up with once that
        baseline ages past the window."""
        user = _make_user(dbsession, "fup_u5")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(10),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id in ids

    def test_recent_signup_not_followed_up(self, dbsession: Session):
        user = _make_user(dbsession, "fup_u6")
        coord = _make_coordinator(dbsession, user.id, last_correspondence_at=_cutoff(1))

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_baseline_only_followed_up_once(self, dbsession: Session):
        """An already-followed-up, still-quiet user is not re-contacted."""
        user = _make_user(dbsession, "fup_u7")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(10),
            last_followup_sent_at=_cutoff(2),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_recently_followed_up_quiet_user_excluded(self, dbsession: Session):
        """Follow-up after the last activity => don't re-fire this quiet spell."""
        user = _make_user(dbsession, "fup_u8")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            last_followup_sent_at=_cutoff(1),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id not in ids

    def test_re_armed_after_fresh_activity(self, dbsession: Session):
        """Engaged after a prior follow-up, then went quiet again => fire again."""
        user = _make_user(dbsession, "fup_u9")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            last_followup_sent_at=_cutoff(20),
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id for c in dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        }
        assert coord.agent_id in ids

    def test_only_personal_coordinator_returned(self, dbsession: Session):
        """Non-coordinator assistants are never returned as candidates."""
        user = _make_user(dbsession, "fup_u10")
        _make_coordinator(dbsession, user.id, last_correspondence_at=_cutoff(8))
        specialist = _make_assistant(
            dbsession,
            user.id,
            first_name="Specialist",
            last_correspondence_at=_cutoff(8),
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        assert specialist.agent_id not in {c.agent_id for c in candidates}
        assert all(c.is_coordinator for c in candidates)

    def test_excludes_demos_by_default(self, dbsession: Session):
        user = _make_user(dbsession, "fup_u11")
        demo_meta = DemoAssistantMeta(
            source_assistant_id=None,
            demoer_user_id=user.id,
            label="demo",
        )
        dbsession.add(demo_meta)
        dbsession.flush()
        demo_coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            demo_id=demo_meta.id,
        )

        dao = AssistantDAO(dbsession)
        without_demo = dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        with_demo = dao.find_followup_candidates(
            followup_cutoff=_cutoff(7),
            include_demo=True,
        )
        assert demo_coord.agent_id not in {c.agent_id for c in without_demo}
        assert demo_coord.agent_id in {c.agent_id for c in with_demo}

    def test_excludes_is_local_by_default(self, dbsession: Session):
        user = _make_user(dbsession, "fup_u12")
        local = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            is_local=True,
        )

        dao = AssistantDAO(dbsession)
        without = dao.find_followup_candidates(followup_cutoff=_cutoff(7))
        with_local = dao.find_followup_candidates(
            followup_cutoff=_cutoff(7),
            include_local=True,
        )
        assert local.agent_id not in {c.agent_id for c in without}
        assert local.agent_id in {c.agent_id for c in with_local}

    def test_respects_limit(self, dbsession: Session):
        for i in range(3):
            user = _make_user(dbsession, f"fup_lim_{i}")
            _make_coordinator(dbsession, user.id, last_correspondence_at=_cutoff(8))

        dao = AssistantDAO(dbsession)
        limited = dao.find_followup_candidates(followup_cutoff=_cutoff(7), limit=2)
        assert len(limited) == 2


# ===========================================================================
# 2. Routine (templated email from twin@, no adapter wake)
# ===========================================================================


class TestInactivityFollowupRoutine:
    @pytest.mark.anyio
    async def test_noop_when_no_candidates(self, dbsession: Session, mock_send):
        result = await run_inactivity_followup(session=dbsession)
        assert isinstance(result, InactivityFollowupResult)
        assert result.followup_candidates_found == 0
        mock_send.assert_not_called()

    @pytest.mark.anyio
    async def test_sends_and_stamps(self, dbsession: Session, mock_send):
        user = _make_user(
            dbsession,
            "rte_u1",
            email="owner@test.com",
            first_name="Olivia",
        )
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
        )

        result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 1
        assert result.followups_failed == 0
        assert result.followups_skipped == 0
        mock_send.assert_awaited_once()
        kwargs = mock_send.await_args.kwargs
        assert kwargs["recipient_email"] == "owner@test.com"
        assert kwargs["owner_first_name"] == "Olivia"
        dbsession.refresh(coord)
        assert coord.last_followup_sent_at is not None

    @pytest.mark.anyio
    async def test_send_false_does_not_stamp(self, dbsession: Session):
        user = _make_user(dbsession, "rte_u4", email="owner@test.com")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
        )

        with patch(_SEND_TARGET, new_callable=AsyncMock) as mock:
            mock.return_value = False
            result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 0
        assert result.followups_failed == 1
        dbsession.refresh(coord)
        assert coord.last_followup_sent_at is None

    @pytest.mark.anyio
    async def test_send_raising_does_not_stamp(self, dbsession: Session):
        user = _make_user(dbsession, "rte_raise", email="owner@test.com")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
        )

        with patch(_SEND_TARGET, new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("gmail unreachable")
            result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 0
        assert result.followups_failed == 1
        dbsession.refresh(coord)
        assert coord.last_followup_sent_at is None

    @pytest.mark.anyio
    async def test_skips_opted_out_coordinator(self, dbsession: Session, mock_send):
        user = _make_user(dbsession, "rte_optout", email="owner@test.com")
        _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(8),
            inactivity_followup_opted_out=True,
        )

        result = await run_inactivity_followup(session=dbsession)

        assert result.followup_candidates_found == 0
        mock_send.assert_not_called()

    @pytest.mark.anyio
    async def test_routine_never_deletes_assistants(
        self,
        dbsession: Session,
        mock_send,
    ):
        """The follow-up routine must not hard-delete or deprovision anyone."""
        user = _make_user(dbsession, "rte_u5", email="owner@test.com")
        coord = _make_coordinator(
            dbsession,
            user.id,
            # Far past the old "cleanup" window — must survive.
            last_correspondence_at=_cutoff(60),
            last_followup_sent_at=_cutoff(60),
        )
        specialist = _make_assistant(
            dbsession,
            user.id,
            first_name="Specialist",
            last_followup_sent_at=_cutoff(60),
        )

        await run_inactivity_followup(session=dbsession)

        assert dbsession.get(Assistant, coord.agent_id) is not None
        assert dbsession.get(Assistant, specialist.agent_id) is not None


# ===========================================================================
# 3. Admin endpoints
# ===========================================================================


class TestAdminInactivityEndpoints:
    @pytest.mark.anyio
    async def test_touch_activity_updates_row(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "api_u1")
        long_ago = _cutoff(10)
        a = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=long_ago,
            last_followup_sent_at=long_ago,
        )
        dbsession.commit()

        resp = await client.post(
            f"/v0/admin/assistant/{a.agent_id}/touch-activity",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["rows_updated"] == 1

        dbsession.refresh(a)
        assert a.last_correspondence_at > long_ago
        assert a.last_followup_sent_at is None

    @pytest.mark.anyio
    async def test_touch_activity_unknown_assistant_returns_404(
        self,
        client: AsyncClient,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        resp = await client.post(
            "/v0/admin/assistant/999999/touch-activity",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_opt_out_then_opt_in(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "api_u2")
        a = _make_coordinator(dbsession, user.id)
        dbsession.commit()

        resp = await client.post(
            f"/v0/admin/assistant/{a.agent_id}/opt-out-followups",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        dbsession.refresh(a)
        assert a.inactivity_followup_opted_out is True

        resp = await client.post(
            f"/v0/admin/assistant/{a.agent_id}/opt-in-followups",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        dbsession.refresh(a)
        assert a.inactivity_followup_opted_out is False

    @pytest.mark.anyio
    async def test_opt_out_unknown_assistant_returns_404(self, client: AsyncClient):
        from orchestra.tests.utils import ADMIN_HEADERS

        resp = await client.post(
            "/v0/admin/assistant/999999/opt-out-followups",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_trigger_routine_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_send,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "api_u3", email="owner@test.com")
        _make_coordinator(dbsession, user.id, last_correspondence_at=_cutoff(8))
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/assistants/inactivity-followup",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "followup_candidates_found" in body
        assert "followups_dispatched" in body
        assert "followups_failed" in body
        assert "followups_skipped" in body


# ===========================================================================
# 4. Welcome + follow-up email templates + send helpers
# ===========================================================================


class TestEmailTemplates:
    def test_welcome_email_is_in_coordinator_voice(self):
        from orchestra.routines.inactivity_notifications import (
            WELCOME_SUBJECT,
            build_coordinator_welcome_email,
        )

        body = build_coordinator_welcome_email(owner_first_name="Olivia")
        normalized = re.sub(r"\s+", " ", body.lower())

        assert "unify" in WELCOME_SUBJECT.lower()
        assert "i'm t-w1n" in normalized
        assert "hi olivia," in normalized
        assert "https://console.unify.ai/" in body
        assert "— t-w1n" in normalized

    def test_welcome_email_handles_missing_first_name(self):
        from orchestra.routines.inactivity_notifications import (
            build_coordinator_welcome_email,
        )

        body = build_coordinator_welcome_email(owner_first_name=None)
        normalized = re.sub(r"\s+", " ", body.lower())
        assert "hi," in normalized

    def test_followup_email_is_soft_check_in(self):
        from orchestra.routines.inactivity_notifications import (
            FOLLOWUP_SUBJECT,
            build_coordinator_inactivity_followup_email,
        )

        body = build_coordinator_inactivity_followup_email(owner_first_name="Olivia")
        normalized = re.sub(r"\s+", " ", body.lower())

        assert "haven't heard from you" in FOLLOWUP_SUBJECT.lower()
        assert "haven't heard from you in a while" in normalized
        assert "anything i can help with" in normalized
        assert "hi olivia," in normalized
        assert "https://console.unify.ai/" in body
        assert "— t-w1n" in normalized
        for banned in ("delet", "suspend", "billing", "terminat", "account will"):
            assert banned not in normalized

    def test_followup_email_handles_missing_first_name(self):
        from orchestra.routines.inactivity_notifications import (
            build_coordinator_inactivity_followup_email,
        )

        body = build_coordinator_inactivity_followup_email(owner_first_name=None)
        normalized = re.sub(r"\s+", " ", body.lower())
        assert "hi," in normalized


class TestWelcomeSendHelper:
    @pytest.mark.anyio
    async def test_send_welcome_routes_through_coordinator_mailbox(self):
        from orchestra.routines import inactivity_notifications as notif

        with patch.object(
            notif,
            "send_coordinator_emails",
            new_callable=AsyncMock,
        ) as mock:
            mock.return_value = True
            sent = await notif.send_coordinator_welcome_email(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
            )

        assert sent is True
        mock.assert_awaited_once()
        recipients, subject, _body = mock.await_args.args
        assert recipients == ["owner@test.com"]
        assert subject == notif.WELCOME_SUBJECT

    @pytest.mark.anyio
    async def test_send_welcome_noops_without_recipient(self):
        from orchestra.routines import inactivity_notifications as notif

        with patch.object(
            notif,
            "send_coordinator_emails",
            new_callable=AsyncMock,
        ) as mock:
            sent = await notif.send_coordinator_welcome_email(
                recipient_email=None,
                owner_first_name="Olivia",
            )

        assert sent is False
        mock.assert_not_called()


class TestFollowupSendHelper:
    @pytest.mark.anyio
    async def test_send_followup_routes_through_coordinator_mailbox(self):
        from orchestra.routines import inactivity_notifications as notif

        with patch.object(
            notif,
            "send_coordinator_emails",
            new_callable=AsyncMock,
        ) as mock:
            mock.return_value = True
            sent = await notif.send_coordinator_inactivity_followup_email(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
            )

        assert sent is True
        mock.assert_awaited_once()
        recipients, subject, body = mock.await_args.args
        assert recipients == ["owner@test.com"]
        assert subject == notif.FOLLOWUP_SUBJECT
        assert "haven't heard from you" in body.lower()

    @pytest.mark.anyio
    async def test_send_followup_noops_without_recipient(self):
        from orchestra.routines import inactivity_notifications as notif

        with patch.object(
            notif,
            "send_coordinator_emails",
            new_callable=AsyncMock,
        ) as mock:
            sent = await notif.send_coordinator_inactivity_followup_email(
                recipient_email=None,
                owner_first_name="Olivia",
            )

        assert sent is False
        mock.assert_not_called()
