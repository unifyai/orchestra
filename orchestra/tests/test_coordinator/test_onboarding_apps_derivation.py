"""Derivation and reset behaviour for the Integrations ``apps`` step."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestra.services import coordinator_service as svc
from orchestra.services import onboarding_graph as graph


def test_connection_updated_after_respects_reset_timestamp() -> None:
    reset_at = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    assert (
        svc._connection_updated_after(reset_at - timedelta(hours=1), reset_at) is False
    )
    assert (
        svc._connection_updated_after(reset_at + timedelta(minutes=1), reset_at) is True
    )
    assert svc._connection_updated_after(None, reset_at) is False


def test_has_connected_integration_ignores_pre_reset_connections() -> None:
    reset_at = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    scope = SimpleNamespace(
        integration_connections=[
            SimpleNamespace(
                status="connected",
                updated_at=reset_at - timedelta(hours=1),
            ),
        ],
    )
    assert svc._has_connected_integration(scope, reset_after=reset_at) is False


def test_has_connected_integration_counts_post_reset_connections() -> None:
    reset_at = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    scope = SimpleNamespace(
        integration_connections=[
            SimpleNamespace(
                status="connected",
                updated_at=reset_at + timedelta(minutes=5),
            ),
        ],
    )
    assert svc._has_connected_integration(scope, reset_after=reset_at) is True


@pytest.mark.anyio
async def test_derive_onboarding_progress_uses_integration_connections_for_apps() -> (
    None
):
    coordinator = SimpleNamespace(
        agent_id=7,
        user_id="user-1",
        organization_id=None,
        is_coordinator=True,
    )
    connected = SimpleNamespace(
        status="connected",
        updated_at=datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
    )
    scope = MagicMock()
    scope.integration_connections = [connected]
    scope.trigger_outbound_created_at.return_value = None
    session = MagicMock()
    with (
        patch.object(
            svc,
            "get_coordinator_state",
            return_value={"onboarding_active": True},
        ),
        patch.object(svc, "_OnboardingProbeScope", return_value=scope),
    ):
        completed = svc.derive_onboarding_progress(
            session,
            coordinator=coordinator,
        )
    assert "apps" in completed


def test_reset_apps_couples_integration_demo_steps() -> None:
    coupled = graph.completion_coupled_steps("apps")
    assert "apps" in coupled
    assert "integration-read" in coupled
    assert "integration-action" in coupled


def test_demo_event_messages_do_not_name_cm_completion_tool() -> None:
    for step_id in graph.DEMO_STEP_IDS:
        step = graph.STEP_BY_ID[step_id]
        assert step.event is not None
        assert "set_onboarding_task_state" not in step.event.message
    connect = graph.chip_event_for("apps", "day-to-day-tools")
    assert connect is not None
    assert "set_onboarding_task_state" not in connect.message
    demo = graph.chip_event_for("integration-read", "crm-pipeline-summary")
    assert demo is not None
    assert "set_onboarding_task_state" not in demo.message
