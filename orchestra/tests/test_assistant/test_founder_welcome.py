"""Tests for the personal founder welcome email (dan@unify.ai)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest


class TestFounderWelcomeTemplate:
    def test_body_is_personal_and_invite_reply(self):
        from orchestra.routines.founder_welcome import (
            FOUNDER_WELCOME_SUBJECT,
            build_founder_welcome_email,
        )

        body = build_founder_welcome_email(owner_first_name="Daniel")
        normalized = re.sub(r"\s+", " ", body.lower())

        assert "dan" in FOUNDER_WELCOME_SUBJECT.lower()
        assert "hey daniel," in normalized
        assert "i'm dan" in normalized
        assert "humans behind unify" in normalized
        assert "👋" in body
        assert "hit reply" in normalized
        assert "real email" in normalized
        assert "read every" in normalized
        assert "🫶" in body
        assert "my the droid be with you!" in normalized
        assert "automated" not in normalized
        assert "t-w1n" not in normalized

    def test_handles_missing_first_name(self):
        from orchestra.routines.founder_welcome import build_founder_welcome_email

        body = build_founder_welcome_email(owner_first_name=None)
        normalized = re.sub(r"\s+", " ", body.lower())
        assert "hey," in normalized


class TestFounderWelcomeSend:
    @pytest.mark.anyio
    async def test_sends_from_dan_mailbox(self):
        from orchestra.routines import founder_welcome as fw

        with (
            patch.object(
                fw,
                "get_founder_welcome_from_email",
                return_value="dan@unify.ai",
            ),
            patch(
                "orchestra.web.api.utils.email.send_email_async_result",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_send.return_value = {"id": "msg-1", "threadId": "thr-1"}
            sent = await fw.send_founder_welcome_email(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
            )

        assert sent is True
        mock_send.assert_awaited_once()
        kwargs = mock_send.await_args.kwargs
        assert kwargs["to_email"] == "owner@test.com"
        assert kwargs["from_email"] == "dan@unify.ai"
        assert kwargs["impersonate_email"] == "dan@unify.ai"
        assert kwargs["email_subject"] == fw.FOUNDER_WELCOME_SUBJECT
        assert "hey olivia," in kwargs["email_body"].lower()

    @pytest.mark.anyio
    async def test_noops_without_recipient(self):
        from orchestra.routines import founder_welcome as fw

        with patch(
            "orchestra.web.api.utils.email.send_email_async_result",
            new_callable=AsyncMock,
        ) as mock_send:
            sent = await fw.send_founder_welcome_email(
                recipient_email=None,
                owner_first_name="Olivia",
            )

        assert sent is False
        mock_send.assert_not_called()

    @pytest.mark.anyio
    async def test_noops_when_disabled(self):
        from orchestra.routines import founder_welcome as fw

        with (
            patch.object(fw, "get_founder_welcome_from_email", return_value=None),
            patch(
                "orchestra.web.api.utils.email.send_email_async_result",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            sent = await fw.send_founder_welcome_email(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
            )

        assert sent is False
        mock_send.assert_not_called()


class TestSignupWelcomeBundle:
    @pytest.mark.anyio
    async def test_sends_both_welcomes_independently(self):
        from orchestra.routines import founder_welcome as fw

        with (
            patch(
                "orchestra.routines.inactivity_notifications.send_coordinator_welcome_email",
                new_callable=AsyncMock,
            ) as twin_send,
            patch.object(
                fw,
                "send_founder_welcome_email",
                new_callable=AsyncMock,
            ) as founder_send,
        ):
            twin_send.return_value = True
            founder_send.return_value = True
            await fw.send_signup_welcome_emails_safe(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
                user_id=42,
            )

        twin_send.assert_awaited_once_with(
            recipient_email="owner@test.com",
            owner_first_name="Olivia",
        )
        founder_send.assert_awaited_once_with(
            recipient_email="owner@test.com",
            owner_first_name="Olivia",
        )

    @pytest.mark.anyio
    async def test_founder_still_sends_if_twin_raises(self):
        from orchestra.routines import founder_welcome as fw

        with (
            patch(
                "orchestra.routines.inactivity_notifications.send_coordinator_welcome_email",
                new_callable=AsyncMock,
                side_effect=RuntimeError("twin mailbox down"),
            ),
            patch.object(
                fw,
                "send_founder_welcome_email",
                new_callable=AsyncMock,
            ) as founder_send,
        ):
            founder_send.return_value = True
            await fw.send_signup_welcome_emails_safe(
                recipient_email="owner@test.com",
                owner_first_name="Olivia",
                user_id=42,
            )

        founder_send.assert_awaited_once()
