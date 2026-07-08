"""Silent onboarding render refresh emissions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.services import coordinator_service as svc

_ACTIVE_STATE = {"onboarding_active": True, "onboarding_step": None}
_RENDER = {
    "active_step_id": "whatsapp-number",
    "steps": [{"id": "whatsapp-number", "status": "done"}],
    "next_targets": [],
}


def _fake_coordinator(agent_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        user_id="user-1",
        organization_id=None,
        is_coordinator=True,
        first_name="T-W1N",
        surname=None,
    )


@pytest.mark.anyio
async def test_render_updated_skips_when_completed_set_unchanged() -> None:
    coordinator = _fake_coordinator()
    baseline = ["email-reference", "email-reply"]
    with (
        patch.object(svc, "get_coordinator_state", return_value=_ACTIVE_STATE),
        patch.object(svc, "derive_onboarding_progress", return_value=baseline),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_onboarding_render_if_changed(
            session=MagicMock(),
            coordinator=coordinator,
            baseline_completed_step_ids=baseline,
        )
    assert result is False
    post.assert_not_called()


@pytest.mark.anyio
async def test_render_updated_emits_when_completed_set_changes() -> None:
    coordinator = _fake_coordinator(agent_id=7)
    baseline = ["email-reference", "email-reply"]
    completed_now = [*baseline, "whatsapp-number"]
    with (
        patch.object(svc, "get_coordinator_state", return_value=_ACTIVE_STATE),
        patch.object(svc, "derive_onboarding_progress", return_value=completed_now),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_onboarding_render_if_changed(
            session=MagicMock(),
            coordinator=coordinator,
            baseline_completed_step_ids=baseline,
            reason="contact_identity_updated",
        )
    assert result is True
    post.assert_awaited_once()
    kwargs = post.await_args.kwargs
    assert kwargs["event_type"] == svc.COORDINATOR_ONBOARDING_EVENT_TYPE
    assert (
        kwargs["extra_event_fields"]["subtype"] == svc.SUBTYPE_ONBOARDING_RENDER_UPDATED
    )
    details = kwargs["extra_event_fields"]["details"]
    assert details["reason"] == "contact_identity_updated"
    assert details["completed_step_ids"] == completed_now
    assert details["newly_completed_step_ids"] == ["whatsapp-number"]
    assert details["newly_uncompleted_step_ids"] == []
    assert details["onboarding"] == _RENDER
