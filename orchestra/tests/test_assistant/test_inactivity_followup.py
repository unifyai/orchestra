"""Tests for the assistant inactivity follow-up + auto-cleanup routine.

Covers:
    1. AssistantDAO inactivity helpers
        - touch_last_correspondence_at updates and clears followup
        - mark_followup_sent / mark_termination_initiated / clear_termination_initiated
        - find_followup_candidates filtering (inactive only, not
          already-followed, not terminated, no demos by default)
        - find_auto_cleanup_candidates covers silent and explicit paths
    2. inactivity_followup routine
        - no-op when no candidates
        - dispatches follow-up and stamps last_followup_sent_at
        - cleans up silent-path + explicit-path assistants
        - leaves the assistant row alive on deprovision errors
        - excludes demo assistants
    3. Admin endpoints
        - touch-activity, terminate, cancel-termination (200 + 404)
        - inactivity-followup trigger endpoint
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    DemoAssistantMeta,
    Organization,
    User,
)
from orchestra.routines.inactivity_followup import (
    InactivityFollowupResult,
    run_inactivity_followup,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(dbsession: Session, uid: str) -> User:
    user = User(id=uid, email=f"{uid}@test.com")
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(
    dbsession: Session,
    user_id: str,
    first_name: str = "InactBot",
    last_correspondence_at: datetime | None = None,
    last_followup_sent_at: datetime | None = None,
    termination_initiated_at: datetime | None = None,
    demo_id: int | None = None,
    is_local: bool = False,
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        last_correspondence_at=last_correspondence_at,
        last_followup_sent_at=last_followup_sent_at,
        termination_initiated_at=termination_initiated_at,
        demo_id=demo_id,
        is_local=is_local,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def _attach_email_contact(
    dbsession: Session,
    assistant_id: int,
    *,
    status: str = "active",
    contact_value: str | None = None,
    contact_type: str = "email",
    provider: str | None = "google_workspace",
) -> AssistantContact:
    """Attach a single AssistantContact row to an existing assistant.

    Used by the no-email fallback tests to control whether the routine
    sees an active email contact, a soft-deleted one, or another
    contact_type entirely.
    """
    c = AssistantContact(
        assistant_id=assistant_id,
        contact_type=contact_type,
        contact_value=contact_value or f"a{assistant_id}@assistant.unify.ai",
        provider=provider,
        provisioned_by="platform",
        status=status,
    )
    dbsession.add(c)
    dbsession.flush()
    return c


@pytest.fixture
def mock_dispatch():
    """Replace the brain-facing dispatch with an AsyncMock."""
    with patch(
        "orchestra.routines.inactivity_followup._dispatch_inactivity_followup_event",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        yield mock


@pytest.fixture
def mock_deprovision():
    """Replace the external deprovisioner — no real infra calls in tests."""
    with patch(
        "orchestra.routines.inactivity_followup.deprovision_assistant_contacts",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = {
            "success": True,
            "attempted": 0,
            "soft_deleted": 0,
            "errors": [],
        }
        yield mock


@pytest.fixture(autouse=True)
def zero_jitter():
    """Disable jitter so tests run instantly."""
    from orchestra.settings import settings

    original = settings.inactivity_followup_jitter_seconds
    settings.inactivity_followup_jitter_seconds = 0
    yield
    settings.inactivity_followup_jitter_seconds = original


# ===========================================================================
# 1. AssistantDAO inactivity helpers
# ===========================================================================


class TestDAOTouchAndMark:
    def test_touch_updates_correspondence_and_clears_followup(
        self,
        dbsession: Session,
    ):
        user = _make_user(dbsession, "dao_u1")
        long_ago = datetime.now(timezone.utc) - timedelta(days=10)
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

    def test_mark_and_clear_termination(self, dbsession: Session):
        user = _make_user(dbsession, "dao_u3")
        a = _make_assistant(dbsession, user.id)

        now = datetime.now(timezone.utc)
        dao = AssistantDAO(dbsession)
        dao.mark_termination_initiated(a.agent_id, now)
        dbsession.flush()
        dbsession.refresh(a)
        assert a.termination_initiated_at == now

        dao.clear_termination_initiated(a.agent_id)
        dbsession.flush()
        dbsession.refresh(a)
        assert a.termination_initiated_at is None


class TestDAOFindFollowupCandidates:
    def test_returns_stale_assistants(self, dbsession: Session):
        user = _make_user(dbsession, "flw_u1")
        stale = _make_assistant(
            dbsession,
            user.id,
            first_name="Stale",
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        fresh = _make_assistant(
            dbsession,
            user.id,
            first_name="Fresh",
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
        )
        ids = {c.agent_id for c in candidates}
        assert stale.agent_id in ids
        assert fresh.agent_id not in ids

    def test_excludes_already_followed(self, dbsession: Session):
        user = _make_user(dbsession, "flw_u2")
        long_ago = datetime.now(timezone.utc) - timedelta(days=5)
        already = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=long_ago,
            last_followup_sent_at=long_ago,
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert already.agent_id not in {c.agent_id for c in candidates}

    def test_excludes_terminated(self, dbsession: Session):
        user = _make_user(dbsession, "flw_u3")
        long_ago = datetime.now(timezone.utc) - timedelta(days=5)
        terminated = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=long_ago,
            termination_initiated_at=datetime.now(timezone.utc),
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert terminated.agent_id not in {c.agent_id for c in candidates}

    def test_excludes_demos_by_default(self, dbsession: Session):
        user = _make_user(dbsession, "flw_u4")
        demo_meta = DemoAssistantMeta(
            source_assistant_id=None,
            demoer_user_id=user.id,
            label="demo",
        )
        dbsession.add(demo_meta)
        dbsession.flush()
        demo = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
            demo_id=demo_meta.id,
        )

        dao = AssistantDAO(dbsession)
        without_demo = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
        )
        with_demo = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
            include_demo=True,
        )
        assert demo.agent_id not in {c.agent_id for c in without_demo}
        assert demo.agent_id in {c.agent_id for c in with_demo}

    def test_respects_limit(self, dbsession: Session):
        user = _make_user(dbsession, "flw_u5")
        long_ago = datetime.now(timezone.utc) - timedelta(days=5)
        for _ in range(3):
            _make_assistant(dbsession, user.id, last_correspondence_at=long_ago)

        dao = AssistantDAO(dbsession)
        limited = dao.find_followup_candidates(
            followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
            limit=2,
        )
        assert len(limited) == 2

    def test_excludes_is_local_by_default(self, dbsession: Session):
        """Local-runtime test assistants must not appear in production runs."""
        user = _make_user(dbsession, "flw_u6")
        long_ago = datetime.now(timezone.utc) - timedelta(days=5)
        local = _make_assistant(
            dbsession,
            user.id,
            first_name="LocalBot",
            last_correspondence_at=long_ago,
            is_local=True,
        )
        prod = _make_assistant(
            dbsession,
            user.id,
            first_name="ProdBot",
            last_correspondence_at=long_ago,
            is_local=False,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id
            for c in dao.find_followup_candidates(
                followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
            )
        }
        assert local.agent_id not in ids
        assert prod.agent_id in ids

    def test_include_local_override(self, dbsession: Session):
        """Tests can opt back in to local assistants via include_local=True."""
        user = _make_user(dbsession, "flw_u7")
        long_ago = datetime.now(timezone.utc) - timedelta(days=5)
        local = _make_assistant(
            dbsession,
            user.id,
            first_name="LocalBot",
            last_correspondence_at=long_ago,
            is_local=True,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id
            for c in dao.find_followup_candidates(
                followup_cutoff=datetime.now(timezone.utc) - timedelta(days=3),
                include_local=True,
            )
        }
        assert local.agent_id in ids


class TestDAOFindAutoCleanupCandidates:
    def test_silent_path(self, dbsession: Session):
        """Assistants with a stale follow-up and no termination qualify."""
        user = _make_user(dbsession, "clu_u1")
        stale_followup = datetime.now(timezone.utc) - timedelta(days=10)
        a = _make_assistant(
            dbsession,
            user.id,
            last_followup_sent_at=stale_followup,
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_auto_cleanup_candidates(
            cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
        )
        assert a.agent_id in {c.agent_id for c in candidates}

    def test_explicit_path(self, dbsession: Session):
        """Assistants with a stale explicit termination qualify."""
        user = _make_user(dbsession, "clu_u2")
        a = _make_assistant(
            dbsession,
            user.id,
            termination_initiated_at=datetime.now(timezone.utc) - timedelta(days=10),
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_auto_cleanup_candidates(
            cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
        )
        assert a.agent_id in {c.agent_id for c in candidates}

    def test_recent_followup_not_included(self, dbsession: Session):
        user = _make_user(dbsession, "clu_u3")
        recent = _make_assistant(
            dbsession,
            user.id,
            last_followup_sent_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        dao = AssistantDAO(dbsession)
        candidates = dao.find_auto_cleanup_candidates(
            cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
        )
        assert recent.agent_id not in {c.agent_id for c in candidates}

    def test_silent_path_excludes_is_local_by_default(self, dbsession: Session):
        """Local-runtime test assistants must not be hard-deleted by the routine."""
        user = _make_user(dbsession, "clu_u4")
        stale_followup = datetime.now(timezone.utc) - timedelta(days=10)
        local = _make_assistant(
            dbsession,
            user.id,
            first_name="LocalBot",
            last_followup_sent_at=stale_followup,
            is_local=True,
        )
        prod = _make_assistant(
            dbsession,
            user.id,
            first_name="ProdBot",
            last_followup_sent_at=stale_followup,
            is_local=False,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id
            for c in dao.find_auto_cleanup_candidates(
                cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
            )
        }
        assert local.agent_id not in ids
        assert prod.agent_id in ids

    def test_explicit_path_excludes_is_local_by_default(self, dbsession: Session):
        """Even with termination_initiated_at set, is_local assistants are skipped."""
        user = _make_user(dbsession, "clu_u5")
        stale_termination = datetime.now(timezone.utc) - timedelta(days=10)
        local = _make_assistant(
            dbsession,
            user.id,
            first_name="LocalBot",
            termination_initiated_at=stale_termination,
            is_local=True,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id
            for c in dao.find_auto_cleanup_candidates(
                cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
            )
        }
        assert local.agent_id not in ids

    def test_include_local_override(self, dbsession: Session):
        """include_local=True surfaces local assistants for tests."""
        user = _make_user(dbsession, "clu_u6")
        stale_followup = datetime.now(timezone.utc) - timedelta(days=10)
        local = _make_assistant(
            dbsession,
            user.id,
            first_name="LocalBot",
            last_followup_sent_at=stale_followup,
            is_local=True,
        )

        dao = AssistantDAO(dbsession)
        ids = {
            c.agent_id
            for c in dao.find_auto_cleanup_candidates(
                cleanup_cutoff=datetime.now(timezone.utc) - timedelta(days=7),
                include_local=True,
            )
        }
        assert local.agent_id in ids


# ===========================================================================
# 2. Routine
# ===========================================================================


class TestInactivityFollowupRoutine:
    @pytest.mark.anyio
    async def test_noop_when_no_candidates(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        result = await run_inactivity_followup(session=dbsession)
        assert isinstance(result, InactivityFollowupResult)
        assert result.followup_candidates_found == 0
        assert result.cleanup_candidates_found == 0
        mock_dispatch.assert_not_called()
        mock_deprovision.assert_not_called()

    @pytest.mark.anyio
    async def test_dispatches_followup_and_stamps(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        user = _make_user(dbsession, "rte_u1")
        stale = _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        # Attach an active email contact so the routine takes the unity
        # dispatch path. The orchestra-side fallback (no-email) path is
        # covered by TestFallbackForAssistantsWithNoEmail.
        _attach_email_contact(dbsession, stale.agent_id)
        dbsession.flush()

        result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 1
        assert result.followups_failed == 0
        mock_dispatch.assert_awaited_once()
        dbsession.refresh(stale)
        assert stale.last_followup_sent_at is not None

    @pytest.mark.anyio
    async def test_cleanup_deletes_silent_assistant(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        user = _make_user(dbsession, "rte_u2")
        stale_followup = datetime.now(timezone.utc) - timedelta(days=10)
        a = _make_assistant(
            dbsession,
            user.id,
            last_followup_sent_at=stale_followup,
        )
        agent_id = a.agent_id

        result = await run_inactivity_followup(session=dbsession)

        assert result.cleanups_completed == 1
        mock_deprovision.assert_awaited_once()
        # Hard-deleted
        assert dbsession.get(Assistant, agent_id) is None

    @pytest.mark.anyio
    async def test_cleanup_skips_hard_delete_on_deprovision_errors(
        self,
        dbsession: Session,
        mock_dispatch,
    ):
        """A failing deprovision must not hard-delete the row."""
        with patch(
            "orchestra.routines.inactivity_followup.deprovision_assistant_contacts",
            new_callable=AsyncMock,
        ) as mock_fail:
            mock_fail.return_value = {
                "success": False,
                "attempted": 1,
                "soft_deleted": 0,
                "errors": ["boom"],
            }

            user = _make_user(dbsession, "rte_u3")
            a = _make_assistant(
                dbsession,
                user.id,
                last_followup_sent_at=datetime.now(timezone.utc) - timedelta(days=10),
            )
            agent_id = a.agent_id

            result = await run_inactivity_followup(session=dbsession)

            assert result.cleanups_completed == 0
            assert result.cleanups_failed == 1
            assert dbsession.get(Assistant, agent_id) is not None


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
        long_ago = datetime.now(timezone.utc) - timedelta(days=10)
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
    async def test_terminate_then_cancel(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "api_u2")
        a = _make_assistant(dbsession, user.id)
        dbsession.commit()

        resp = await client.post(
            f"/v0/admin/assistant/{a.agent_id}/terminate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        dbsession.refresh(a)
        assert a.termination_initiated_at is not None

        resp = await client.post(
            f"/v0/admin/assistant/{a.agent_id}/cancel-termination",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        dbsession.refresh(a)
        assert a.termination_initiated_at is None

    @pytest.mark.anyio
    async def test_trigger_routine_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "api_u3")
        _make_assistant(
            dbsession,
            user.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/assistants/inactivity-followup",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "followup_candidates_found" in body
        assert "cleanup_candidates_found" in body


# ===========================================================================
# 4. Dispatch helper (HTTP shape)
# ===========================================================================


class TestDispatchHelper:
    @pytest.mark.anyio
    async def test_dispatch_posts_to_adapter_webhook(self):
        """Verify the helper builds the correct URL / headers / body."""
        from orchestra.routines import inactivity_followup as rt

        fake_response = MagicMock(status_code=200)
        fake_response.raise_for_status.return_value = None

        async def _fake_post(url, json=None, headers=None, timeout=None):
            _fake_post.url = url
            _fake_post.json = json
            _fake_post.headers = headers
            return fake_response

        fake_client = MagicMock()
        fake_client.post = AsyncMock(side_effect=_fake_post)

        with (
            patch(
                "orchestra.web.api.utils.assistant_infra.ADMIN_KEY",
                "test-admin-key",
            ),
            patch(
                "orchestra.web.api.utils.assistant_infra._adapters_url_for",
                return_value="http://adapters.test",
            ),
            patch(
                "orchestra.web.api.utils.http_client.get_async_client",
                return_value=fake_client,
            ),
        ):
            await rt._dispatch_inactivity_followup_event(
                agent_id=42,
                deploy_env=None,
            )

        fake_client.post.assert_awaited_once()
        kwargs = fake_client.post.await_args.kwargs
        args = fake_client.post.await_args.args
        called_url = args[0] if args else kwargs.get("url")
        assert called_url == "http://adapters.test/assistant/inactivity-followup"
        assert kwargs["json"] == {"assistant_id": "42"}
        assert kwargs["headers"] == {"Authorization": "Bearer test-admin-key"}

    @pytest.mark.anyio
    async def test_dispatch_no_ops_without_admin_key(self):
        """Missing config => warning + return, no HTTP call."""
        from orchestra.routines import inactivity_followup as rt

        fake_client = MagicMock()
        fake_client.post = AsyncMock()

        with (
            patch("orchestra.web.api.utils.assistant_infra.ADMIN_KEY", None),
            patch(
                "orchestra.web.api.utils.assistant_infra._adapters_url_for",
                return_value="http://adapters.test",
            ),
            patch(
                "orchestra.web.api.utils.http_client.get_async_client",
                return_value=fake_client,
            ),
        ):
            await rt._dispatch_inactivity_followup_event(
                agent_id=42,
                deploy_env=None,
            )

        fake_client.post.assert_not_awaited()


# ===========================================================================
# 5. Inactivity deletion notification (Fix C)
# ===========================================================================


def _make_user_with_email(
    dbsession: Session,
    uid: str,
    email: str,
    first_name: str | None = None,
) -> User:
    # Note: User column for the salutation/first name is `name`, not
    # `first_name`. Helper kwarg stays `first_name` for readability at
    # call sites; only the SQLAlchemy attribute we set differs.
    user = User(id=uid, email=email, name=first_name)
    dbsession.add(user)
    dbsession.flush()
    return user


class TestInactivityDeletionNotification:
    """Cleanup must notify the assistant's lifecycle owner before hard-delete."""

    @pytest.mark.anyio
    async def test_personal_assistant_notifies_user_id_owner(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "del_personal_owner",
            "owner@test.com",
            first_name="Olivia",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            first_name="Bert",
            last_followup_sent_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(recipients, subject, body):
            sent.append((recipients, subject, body))

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            result = await run_inactivity_followup(session=dbsession)

        assert result.cleanups_completed == 1
        assert len(sent) == 1
        recipients, subject, _body = sent[0]
        assert recipients == ["owner@test.com"]
        assert "removed" in subject.lower()
        # Hard-delete also went through
        assert dbsession.get(Assistant, a.agent_id) is None

    @pytest.mark.anyio
    async def test_org_assistant_notifies_creator_not_org_owner(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        """Org assistant must notify Assistant.user_id (the creator),
        NOT the organization's owner_id user."""
        org_owner = _make_user_with_email(
            dbsession,
            "del_org_root",
            "org-owner@test.com",
        )
        creator = _make_user_with_email(
            dbsession,
            "del_org_creator",
            "creator@test.com",
            first_name="Cara",
        )
        org = Organization(name="DelOrg", owner_id=org_owner.id)
        dbsession.add(org)
        dbsession.flush()

        a = _make_assistant(
            dbsession,
            creator.id,
            first_name="Bert",
            last_followup_sent_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        a.organization_id = org.id
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(recipients, subject, body):
            sent.append((recipients, subject, body))

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            await run_inactivity_followup(session=dbsession)

        assert len(sent) == 1
        recipients, _subj, _body = sent[0]
        assert recipients == ["creator@test.com"]
        assert "org-owner@test.com" not in recipients

    @pytest.mark.anyio
    async def test_deletion_proceeds_when_notification_fails(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        """A notification send failure must not block hard-delete."""
        owner = _make_user_with_email(
            dbsession,
            "del_fail_owner",
            "owner-fail@test.com",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            last_followup_sent_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        dbsession.flush()

        async def _raise(*_args, **_kwargs):
            raise RuntimeError("smtp blew up")

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_raise,
        ):
            result = await run_inactivity_followup(session=dbsession)

        assert result.cleanups_completed == 1
        assert dbsession.get(Assistant, a.agent_id) is None

    def test_email_body_contains_required_phrases(self):
        """Lock the requested copy contract into a regression test."""
        import re

        from orchestra.routines.inactivity_notifications import build_deletion_email

        assistant = Assistant(
            agent_id=42,
            user_id="x",
            first_name="Bert",
            surname="Bot",
        )
        body = build_deletion_email(
            assistant=assistant,
            owner_first_name="Olivia",
            days=10,
        )
        # The HTML body line-wraps the copy; collapse whitespace before
        # substring matching so future formatting tweaks don't break the
        # phrase contract.
        normalized = re.sub(r"\s+", " ", body.lower())
        assert "previously followed up" in normalized
        assert (
            "didn't respond or chose to not keep" in normalized
        ), "exact product-brief phrasing must be present"
        assert "10 days" in normalized
        assert "bert" in normalized
        assert "olivia" in normalized


# ===========================================================================
# 6. Fallback follow-up for assistants with no provisioned email
# ===========================================================================


class TestFallbackForAssistantsWithNoEmail:
    """Stage-1 dispatch must branch on whether the assistant has its own
    email. With email → wake unity (current path). Without → orchestra
    sends the first-person console-redirect from hello@unify.ai."""

    @pytest.mark.anyio
    async def test_assistant_with_email_dispatches_to_unity(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "fb_with_email",
            "owner@test.com",
            first_name="Olivia",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        _attach_email_contact(dbsession, a.agent_id)
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(*_a, **_kw):
            sent.append(_a)

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 1
        mock_dispatch.assert_awaited_once()
        assert sent == []
        dbsession.refresh(a)
        assert a.last_followup_sent_at is not None

    @pytest.mark.anyio
    async def test_assistant_without_email_routes_through_general_address(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "fb_no_email",
            "owner@test.com",
            first_name="Olivia",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            first_name="Bert",
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        # No AssistantContact row at all.
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(recipients, subject, body):
            sent.append((recipients, subject, body))

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            result = await run_inactivity_followup(session=dbsession)

        assert result.followups_dispatched == 1
        mock_dispatch.assert_not_awaited()
        assert len(sent) == 1
        recipients, subject, _body = sent[0]
        assert recipients == ["owner@test.com"]
        assert "Bert" in subject
        dbsession.refresh(a)
        assert a.last_followup_sent_at is not None

    @pytest.mark.anyio
    async def test_only_deleted_email_counts_as_no_email(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "fb_deleted_email",
            "owner@test.com",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        _attach_email_contact(dbsession, a.agent_id, status="deleted")
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(*_a, **_kw):
            sent.append(_a)

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            await run_inactivity_followup(session=dbsession)

        mock_dispatch.assert_not_awaited()
        assert len(sent) == 1  # fallback fired

    @pytest.mark.anyio
    async def test_phone_only_assistant_uses_console_redirect(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "fb_phone_only",
            "owner@test.com",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        _attach_email_contact(
            dbsession,
            a.agent_id,
            contact_type="phone",
            contact_value="+15551112222",
            provider="twilio",
        )
        dbsession.flush()

        sent: list[tuple] = []

        async def _capture(*_a, **_kw):
            sent.append(_a)

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_capture,
        ):
            await run_inactivity_followup(session=dbsession)

        mock_dispatch.assert_not_awaited()
        assert len(sent) == 1  # fallback fired

    @pytest.mark.anyio
    async def test_console_redirect_send_failure_does_not_mark_followup_sent(
        self,
        dbsession: Session,
        mock_dispatch,
        mock_deprovision,
    ):
        owner = _make_user_with_email(
            dbsession,
            "fb_send_fail",
            "owner@test.com",
        )
        a = _make_assistant(
            dbsession,
            owner.id,
            last_correspondence_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        # No email contact → fallback path.
        dbsession.flush()

        async def _raise(*_args, **_kwargs):
            raise RuntimeError("smtp blew up")

        with patch(
            "orchestra.routines.assistant_contact_notifications.send_notification_emails",
            side_effect=_raise,
        ):
            result = await run_inactivity_followup(session=dbsession)

        assert result.followups_failed == 1
        assert result.followups_dispatched == 0
        dbsession.refresh(a)
        assert a.last_followup_sent_at is None

    def test_console_redirect_body_is_in_assistant_voice(self):
        """Body uses the assistant's first-person voice, redirects to
        console, and includes the no-reply line. No Unify-team
        attribution; no email/whatsapp explanations."""
        import re

        from orchestra.routines.inactivity_notifications import (
            build_console_redirect_email,
        )

        assistant = Assistant(
            agent_id=42,
            user_id="x",
            first_name="Bert",
            surname="Bot",
        )
        body = build_console_redirect_email(
            assistant=assistant,
            owner_first_name="Olivia",
        )
        normalized = re.sub(r"\s+", " ", body.lower())

        # First-person voice markers
        assert "it's bert" in normalized
        assert "i noticed" in normalized
        assert "— bert" in normalized

        # Console redirect + no-reply nudge
        assert "https://console.unify.ai/" in body
        assert "please don't reply" in normalized
        assert "chat with me on the console" in normalized

        # Locks in the cleaner copy: no Unify-team attribution, no
        # explanation of why the email is routed differently.
        assert "unify team" not in normalized
        assert "don't have my own email" not in normalized
        assert "whatsapp" not in normalized
        assert "olivia" in normalized

    def test_console_redirect_subject_uses_assistant_first_name(self):
        from orchestra.routines.inactivity_notifications import (
            CONSOLE_REDIRECT_SUBJECT_TEMPLATE,
        )

        rendered = CONSOLE_REDIRECT_SUBJECT_TEMPLATE.format(first_name="Bert")
        assert rendered == "Hi from Bert on Unify"
