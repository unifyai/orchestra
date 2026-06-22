"""Unit tests for the Coordinator onboarding narration helpers.

Exercises the pure gating + payload-shaping logic against in-memory
mocks rather than the full FastAPI stack, since the network round
trip and orchestra-side state read are both already covered by
upstream integration tests. The point of these tests is to pin down
the **contract** between trigger sites and Droid:

* the helper stays silent unless ``Coordinator/State.mode ==
  'onboarding'``;
* it refuses unknown subtypes;
* the wire payload matches what the adapters webhook expects;
* the sibling-assistant resolver lands on the right Coordinator;
* sync and async flavours behave the same under the gate.

Adapters HTTP failures are pinned via the test that mocks
``_post_droid_system_event`` to raise — the helper should swallow
and return ``False`` so the wrapping endpoint never regresses.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestra.services import coordinator_service as svc

ONBOARDING_STATE = {"mode": "onboarding", "onboarding_step": None}
WORKING_STATE = {"mode": "working", "onboarding_step": None}

# Sentinel onboarding render attached to every emitted event by
# ``_with_onboarding_render``. Patched in so payload assertions stay
# focused on the per-subtype fields rather than re-deriving the graph.
_RENDER = {"active_step_id": None, "steps": [], "next_targets": []}


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
        first_name="Twin",
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
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
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
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
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
    # payload stays compact and JSON-clean; every event also carries the
    # precomputed onboarding render for the brains.
    assert kwargs["extra_event_fields"] == {
        "subtype": svc.SUBTYPE_INTEGRATION_CONNECTED,
        "details": {"secret_name": "SLACK_TOKEN", "onboarding": _RENDER},
    }


@pytest.mark.anyio
async def test_async_notify_rejects_unknown_subtype() -> None:
    """The subtype taxonomy is closed — anything else short-circuits."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
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
        patch.object(svc, "_post_droid_system_event", new=failing_post),
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
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
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
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
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


@pytest.mark.anyio
async def test_step_skipped_event_embeds_step_snapshots() -> None:
    """Skip events tell Droid which step was skipped and what is resolved so far."""
    coordinator = _fake_coordinator(agent_id=15)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_step_skipped_event(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="workspace",
            completed_step_ids=["apps"],
            skipped_step_ids=["workspace"],
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_SKIPPED,
        "details": {
            "step_id": "workspace",
            "completed_step_ids": ["apps"],
            "skipped_step_ids": ["workspace"],
            "onboarding": _RENDER,
        },
    }


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
    """GOOGLE_*/MICROSOFT_*/AZURE_* secrets must route to the workspace subtype."""
    subtype, msg = svc._classify_secret_for_onboarding("GOOGLE_REFRESH_TOKEN")
    assert subtype == svc.SUBTYPE_WORKSPACE_CONNECTED
    assert "Google workspace" in msg

    subtype, msg = svc._classify_secret_for_onboarding("microsoft_access_token")
    # Case-insensitive on the prefix check so adapters that lowercase
    # secret names don't slip past as generic integrations.
    assert subtype == svc.SUBTYPE_WORKSPACE_CONNECTED
    assert "Microsoft workspace" in msg

    subtype, msg = svc._classify_secret_for_onboarding("AZURE_ACCESS_TOKEN")
    assert subtype == svc.SUBTYPE_WORKSPACE_CONNECTED
    assert "Microsoft workspace" in msg


def test_classify_secret_generic_name_yields_integration_subtype() -> None:
    """Non-workspace secrets fall back to the integration subtype."""
    subtype, msg = svc._classify_secret_for_onboarding("SLACK_BOT_TOKEN")
    assert subtype == svc.SUBTYPE_INTEGRATION_CONNECTED
    assert "SLACK_BOT_TOKEN" in msg


def test_derive_onboarding_progress_orders_steps_canonically() -> None:
    """Derivation composes the per-step checks in checklist order."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "_has_email_reply", return_value=True),
        patch.object(svc, "_has_user_whatsapp_number", return_value=False),
        patch.object(svc, "_has_whatsapp_message", return_value=True),
        patch.object(svc, "_has_whatsapp_call", return_value=False),
        patch.object(svc, "_has_user_phone_number", return_value=True),
        patch.object(svc, "_has_sms_message", return_value=False),
        patch.object(svc, "_has_phone_call", return_value=True),
        patch.object(svc, "_has_slack_install", return_value=True),
        patch.object(svc, "_has_slack_message", return_value=False),
        patch.object(svc, "_has_discord_connection", return_value=True),
        patch.object(svc, "_has_discord_message", return_value=False),
        patch.object(svc, "_has_workspace_email", return_value=True),
        patch.object(svc, "_has_app_secret", return_value=False),
        patch.object(svc, "_has_root_action", return_value=True),
        patch.object(svc, "_has_scheduled_task", return_value=True),
    ):
        derived = svc.derive_onboarding_progress(
            MagicMock(),
            coordinator=coordinator,
        )
    assert derived == [
        svc.ONBOARDING_STEP_EMAIL_REPLY,
        svc.ONBOARDING_STEP_WHATSAPP_MESSAGE,
        svc.ONBOARDING_STEP_PHONE_NUMBER,
        svc.ONBOARDING_STEP_PHONE_CALL,
        svc.ONBOARDING_STEP_SLACK_CONNECT,
        svc.ONBOARDING_STEP_DISCORD_CONNECT,
        svc.ONBOARDING_STEP_WORKSPACE,
        svc.ONBOARDING_STEP_ACT,
        svc.ONBOARDING_STEP_SCHEDULE,
    ]


@pytest.mark.anyio
async def test_step_started_event_embeds_active_step_snapshot() -> None:
    """Active-step events tell Droid which checklist row the user selected."""
    coordinator = _fake_coordinator(agent_id=16)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_step_started_event(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="email-reply",
            completed_step_ids=["meet"],
            skipped_step_ids=["phone-call"],
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_STARTED,
        "details": {
            "step_id": "email-reply",
            "completed_step_ids": ["meet"],
            "skipped_step_ids": ["phone-call"],
            "onboarding": _RENDER,
        },
    }


@pytest.mark.anyio
async def test_session_started_event_embeds_server_derived_steps() -> None:
    """The picker event carries the server-derived completion snapshot.

    This is the contract that fixes pre-completed steps: a workspace
    connected in an earlier session never fires a transition event,
    so the opener relies entirely on this derivation being attached.
    """
    coordinator = _fake_coordinator(agent_id=11)
    with (
        patch.object(
            svc,
            "get_coordinator_state",
            return_value={"mode": "onboarding", "skipped_step_ids": ["schedule"]},
        ),
        patch.object(
            svc,
            "derive_onboarding_progress",
            return_value=["workspace", "apps"],
        ) as derive,
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_session_started_event(
            session=MagicMock(),
            coordinator=coordinator,
            medium="chat",
        )
    assert result is True
    derive.assert_called_once()
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields["subtype"] == svc.SUBTYPE_ONBOARDING_SESSION_STARTED
    assert fields["details"] == {
        "medium": "chat",
        "completed_step_ids": ["workspace", "apps"],
        "skipped_step_ids": ["schedule"],
        "onboarding": _RENDER,
    }


@pytest.mark.anyio
async def test_session_started_event_omits_empty_step_snapshot() -> None:
    """A fresh workspace produces a compact payload without an empty list."""
    coordinator = _fake_coordinator(agent_id=12)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ONBOARDING_STATE),
        patch.object(svc, "derive_onboarding_progress", return_value=[]),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_droid_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_session_started_event(
            session=MagicMock(),
            coordinator=coordinator,
            medium="chat",
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields["details"] == {"medium": "chat", "onboarding": _RENDER}
