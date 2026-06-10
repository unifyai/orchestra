"""Unit tests for the Coordinator onboarding narration helpers.

Exercises the pure gating + payload-shaping logic against in-memory
mocks rather than the full FastAPI stack, since the network round
trip and orchestra-side state read are both already covered by
upstream integration tests. The point of these tests is to pin down
the **contract** between trigger sites and Unity:

* the helper stays silent unless ``Coordinator/State.mode ==
  'onboarding'``;
* it refuses unknown subtypes;
* the wire payload matches what the adapters webhook expects;
* the sibling-assistant resolver lands on the right Coordinator;
* sync and async flavours behave the same under the gate.

Adapters HTTP failures are pinned via the test that mocks
``_post_unity_system_event`` to raise — the helper should swallow
and return ``False`` so the wrapping endpoint never regresses.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.services import coordinator_service as svc

ONBOARDING_STATE = {"mode": "onboarding", "onboarding_step": None}
WORKING_STATE = {"mode": "working", "onboarding_step": None}


def _fake_coordinator(agent_id: int = 1, *, is_coord: bool = True) -> SimpleNamespace:
    """Lightweight stand-in for the ORM ``Assistant`` row.

    Only the attributes the service helper actually reads are set —
    everything else stays unset on purpose so a future regression
    that starts touching extra columns surfaces as an
    ``AttributeError`` instead of silently returning stale data.
    """
    return SimpleNamespace(
        agent_id=agent_id,
        user_id="user-1",
        organization_id=None,
        is_coordinator=is_coord,
        first_name="Marty",
        surname=None,
    )


def _fake_specialist(agent_id: int = 2) -> SimpleNamespace:
    """Stand-in for a non-coordinator sibling assistant.

    Used to drive the ``maybe_notify_for_assistant_*`` resolution
    path that must look up the workspace's Coordinator from
    ``(user_id, organization_id)`` rather than emitting on the
    triggering assistant itself.
    """
    return SimpleNamespace(
        agent_id=agent_id,
        user_id="user-1",
        organization_id=None,
        is_coordinator=False,
        first_name="Specialist",
        surname="One",
    )


@pytest.mark.anyio
async def test_async_notify_skips_when_mode_is_working() -> None:
    """Working-mode Coordinators must stay silent — that's the point of the gate."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=WORKING_STATE),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_coordinator_onboarding_event(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="should not be sent",
        )
    assert result is False
    post.assert_not_called()


@pytest.mark.anyio
async def test_async_notify_emits_when_mode_is_onboarding() -> None:
    """Onboarding-mode Coordinators get the event with the canonical payload shape."""
    coordinator = _fake_coordinator(agent_id=42)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_coordinator_onboarding_event(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="user just connected Slack",
            details={"secret_name": "SLACK_TOKEN", "noise": None},
        )
    assert result is True
    post.assert_awaited_once()
    kwargs = post.await_args.kwargs
    assert kwargs["assistant_id"] == 42
    assert kwargs["event_type"] == svc.COORDINATOR_ONBOARDING_EVENT_TYPE
    assert kwargs["message"] == "user just connected Slack"
    # ``None`` values in ``details`` are stripped so the published
    # payload stays compact and JSON-clean.
    assert kwargs["extra_event_fields"] == {
        "subtype": svc.SUBTYPE_INTEGRATION_CONNECTED,
        "details": {"secret_name": "SLACK_TOKEN"},
    }


@pytest.mark.anyio
async def test_async_notify_rejects_unknown_subtype() -> None:
    """The subtype taxonomy is closed — anything else short-circuits."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_coordinator_onboarding_event(
            session=MagicMock(),
            coordinator=coordinator,
            subtype="made_up_subtype",
            message="nope",
        )
    assert result is False
    post.assert_not_called()


@pytest.mark.anyio
async def test_async_notify_swallows_transport_failures() -> None:
    """Adapters outages must never bubble up to the wrapping endpoint."""
    coordinator = _fake_coordinator()
    failing_post = AsyncMock(side_effect=RuntimeError("adapters down"))
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_post_unity_system_event", new=failing_post),
    ):
        result = await svc.notify_coordinator_onboarding_event(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=svc.SUBTYPE_WORKSPACE_CONNECTED,
            message="should swallow",
        )
    assert result is False
    failing_post.assert_awaited_once()


@pytest.mark.anyio
async def test_async_notify_for_assistant_resolves_workspace_coordinator() -> None:
    """A sibling-triggered emit lands on the workspace Coordinator, not the sibling."""
    specialist = _fake_specialist(agent_id=99)
    coordinator = _fake_coordinator(agent_id=7)
    with (
        patch.object(
            svc,
            "get_workspace_coordinator",
            return_value=coordinator,
        ) as resolve,
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.maybe_notify_for_assistant_async(
            session=MagicMock(),
            assistant=specialist,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="sibling secret just landed",
        )
    assert result is True
    resolve.assert_called_once()
    # The published event must target the Coordinator's id (7), not the
    # specialist's (99) — the whole point of the resolver.
    assert post.await_args.kwargs["assistant_id"] == 7


@pytest.mark.anyio
async def test_async_notify_for_assistant_returns_false_when_no_coordinator() -> None:
    """Scopes without a Coordinator must short-circuit cleanly."""
    specialist = _fake_specialist()
    with (
        patch.object(svc, "get_workspace_coordinator", return_value=None),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.maybe_notify_for_assistant_async(
            session=MagicMock(),
            assistant=specialist,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="orphan event",
        )
    assert result is False
    post.assert_not_called()


def test_sync_notify_kicks_a_daemon_thread_when_in_onboarding() -> None:
    """Sync wrapper must spawn a thread instead of blocking on httpx."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_fire_and_forget_onboarding_event") as fire,
    ):
        result = svc.notify_coordinator_onboarding_event_safe_sync(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="user just connected slack",
        )
    assert result is True
    fire.assert_called_once()
    payload = fire.call_args.args[0]
    assert payload["event_type"] == svc.COORDINATOR_ONBOARDING_EVENT_TYPE
    assert payload["extra_event_fields"]["subtype"] == svc.SUBTYPE_INTEGRATION_CONNECTED


def test_sync_notify_silent_when_mode_is_working() -> None:
    """Gate applies symmetrically across sync + async variants."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=WORKING_STATE),
        patch.object(svc, "_fire_and_forget_onboarding_event") as fire,
    ):
        result = svc.notify_coordinator_onboarding_event_safe_sync(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=svc.SUBTYPE_INTEGRATION_CONNECTED,
            message="should not be sent",
        )
    assert result is False
    fire.assert_not_called()


def test_classify_secret_workspace_prefix_yields_workspace_subtype() -> None:
    """GOOGLE_*/MICROSOFT_* secrets must route to the workspace subtype."""
    subtype, msg = svc._classify_secret_for_onboarding("GOOGLE_REFRESH_TOKEN")
    assert subtype == svc.SUBTYPE_WORKSPACE_CONNECTED
    assert "Google workspace" in msg

    subtype, msg = svc._classify_secret_for_onboarding("microsoft_access_token")
    # Case-insensitive on the prefix check so adapters that lowercase
    # secret names don't slip past as generic integrations.
    assert subtype == svc.SUBTYPE_WORKSPACE_CONNECTED
    assert "Microsoft workspace" in msg


def test_classify_secret_generic_name_yields_integration_subtype() -> None:
    """Non-workspace secrets fall back to the integration subtype."""
    subtype, msg = svc._classify_secret_for_onboarding("SLACK_BOT_TOKEN")
    assert subtype == svc.SUBTYPE_INTEGRATION_CONNECTED
    assert "SLACK_BOT_TOKEN" in msg
