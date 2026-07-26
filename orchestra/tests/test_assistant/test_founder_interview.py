"""Tests for automated founder interview asks (dan@ + Cal.com)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import Assistant, User
from orchestra.routines.founder_interview_ask import run_founder_interview_ask

_SEND_TARGET = "orchestra.routines.founder_interview.send_founder_interview_email"


def _make_user(
    dbsession: Session,
    uid: str,
    *,
    email: str = "",
    first_name: str | None = "Olivia",
    created_at: datetime | None = None,
) -> User:
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        name=first_name,
        created_at=created_at or (datetime.now(timezone.utc) - timedelta(days=14)),
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_coordinator(
    dbsession: Session,
    user_id: str,
    *,
    last_correspondence_at: datetime | None = None,
    has_engaged: bool = False,
    asked_at: datetime | None = None,
    is_local: bool = False,
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name="T-W1N",
        is_coordinator=True,
        organization_id=None,
        is_local=is_local,
        last_correspondence_at=last_correspondence_at,
        inactivity_followup_has_engaged=has_engaged,
        founder_interview_asked_at=asked_at,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@pytest.fixture(autouse=True)
def zero_jitter_and_enable():
    from orchestra.settings import settings

    orig_jitter = settings.founder_interview_jitter_seconds
    orig_enabled = settings.founder_interview_enabled
    settings.founder_interview_jitter_seconds = 0
    settings.founder_interview_enabled = True
    yield
    settings.founder_interview_jitter_seconds = orig_jitter
    settings.founder_interview_enabled = orig_enabled


@pytest.fixture
def mock_send():
    with patch(_SEND_TARGET, new_callable=AsyncMock) as mock:
        mock.return_value = True
        yield mock


class TestInterviewTemplates:
    def test_engaged_quiet_includes_cal_and_voice(self):
        from orchestra.routines.founder_interview import (
            build_founder_interview_email,
            founder_interview_subject,
        )

        body = build_founder_interview_email(
            owner_first_name="Daniel",
            variant="engaged_quiet",
            cal_url="https://cal.com/team/unify/chat",
        )
        normalized = re.sub(r"\s+", " ", body.lower())
        assert "15 minutes" in founder_interview_subject("engaged_quiet").lower()
        assert "hey daniel," in normalized
        assert "went quiet" in normalized
        assert "https://cal.com/team/unify/chat" in body
        assert "👋" in body
        assert "🫶" in body
        assert "my the droid be with you!" in normalized

    def test_never_engaged_and_active_variants(self):
        from orchestra.routines.founder_interview import build_founder_interview_email

        never = build_founder_interview_email(
            owner_first_name="Sam",
            variant="never_engaged",
        )
        active = build_founder_interview_email(
            owner_first_name="Sam",
            variant="engaged_active",
        )
        assert "hoping" in never.lower()
        assert "what's working" in active.lower() or "what isnt" in re.sub(
            r"\s+",
            " ",
            active.lower(),
        )


class TestInterviewDAO:
    def test_finds_engaged_quiet_never_and_active(self, dbsession: Session):
        quiet_user = _make_user(dbsession, "iq_quiet")
        never_user = _make_user(dbsession, "iq_never")
        active_user = _make_user(dbsession, "iq_active")
        too_new = _make_user(
            dbsession,
            "iq_new",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        already = _make_user(dbsession, "iq_done")

        _make_coordinator(
            dbsession,
            quiet_user.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        _make_coordinator(
            dbsession,
            never_user.id,
            last_correspondence_at=_cutoff(6),
            has_engaged=False,
        )
        _make_coordinator(
            dbsession,
            active_user.id,
            last_correspondence_at=_cutoff(0),
            has_engaged=True,
        )
        _make_coordinator(
            dbsession,
            too_new.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        _make_coordinator(
            dbsession,
            already.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
            asked_at=_cutoff(1),
        )
        dbsession.flush()

        now = datetime.now(timezone.utc)
        rows = AssistantDAO(dbsession).find_founder_interview_candidates(
            now=now,
            min_account_age_days=3,
            quiet_min_days=3,
            never_engaged_min_days=5,
            active_min_account_age_days=7,
            active_recent_days=2,
            include_local=True,
        )
        by_variant = {variant: assistant.user_id for assistant, variant in rows}
        assert by_variant.get("engaged_quiet") == quiet_user.id
        assert by_variant.get("never_engaged") == never_user.id
        assert by_variant.get("engaged_active") == active_user.id
        assert too_new.id not in {a.user_id for a, _ in rows}
        assert already.id not in {a.user_id for a, _ in rows}

    def test_mark_asked_is_oneshot(self, dbsession: Session):
        user = _make_user(dbsession, "iq_mark")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        dao = AssistantDAO(dbsession)
        now = datetime.now(timezone.utc)
        assert (
            dao.mark_founder_interview_asked(
                coord.agent_id,
                now,
                variant="engaged_quiet",
            )
            == 1
        )
        dbsession.flush()
        dbsession.refresh(coord)
        assert coord.founder_interview_asked_at == now
        assert coord.founder_interview_ask_variant == "engaged_quiet"
        assert (
            dao.mark_founder_interview_asked(
                coord.agent_id,
                now + timedelta(hours=1),
                variant="engaged_active",
            )
            == 0
        )


class TestInterviewRoutine:
    @pytest.mark.anyio
    async def test_dispatches_and_stamps(self, dbsession: Session, mock_send):
        user = _make_user(dbsession, "iq_run", email="owner@test.com")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        dbsession.commit()

        result = await run_founder_interview_ask(session=dbsession)
        assert result.interview_candidates_found >= 1
        assert result.interviews_dispatched >= 1
        mock_send.assert_awaited()
        dbsession.refresh(coord)
        assert coord.founder_interview_asked_at is not None
        assert coord.founder_interview_ask_variant == "engaged_quiet"

    @pytest.mark.anyio
    async def test_failed_send_does_not_stamp(self, dbsession: Session):
        user = _make_user(dbsession, "iq_fail", email="owner@test.com")
        coord = _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        dbsession.commit()

        with patch(_SEND_TARGET, new_callable=AsyncMock, return_value=False):
            result = await run_founder_interview_ask(session=dbsession)

        assert result.interviews_failed >= 1
        dbsession.refresh(coord)
        assert coord.founder_interview_asked_at is None

    @pytest.mark.anyio
    async def test_admin_endpoint(
        self,
        client: AsyncClient,
        dbsession: Session,
        mock_send,
    ):
        from orchestra.tests.utils import ADMIN_HEADERS

        user = _make_user(dbsession, "iq_api", email="owner@test.com")
        _make_coordinator(
            dbsession,
            user.id,
            last_correspondence_at=_cutoff(5),
            has_engaged=True,
        )
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/assistants/founder-interview-ask",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "interview_candidates_found" in body
        assert "interviews_dispatched" in body


class TestInterviewSendHelper:
    @pytest.mark.anyio
    async def test_sends_from_dan_with_cal_link(self):
        from orchestra.routines import founder_interview as fi

        with (
            patch.object(
                fi,
                "get_founder_interview_from_email",
                return_value="dan@unify.ai",
            ),
            patch(
                "orchestra.web.api.utils.email.send_email_async_result",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_send.return_value = {"id": "m1"}
            sent = await fi.send_founder_interview_email(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
                variant="engaged_quiet",
            )

        assert sent is True
        kwargs = mock_send.await_args.kwargs
        assert kwargs["from_email"] == "dan@unify.ai"
        assert kwargs["impersonate_email"] == "dan@unify.ai"
        assert "https://cal.com/team/unify/chat" in kwargs["email_body"]
