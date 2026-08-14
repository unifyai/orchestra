"""Unit tests for the Coordinator onboarding narration helpers.

Exercises the pure gating + payload-shaping logic against in-memory
mocks rather than the full FastAPI stack, since the network round
trip and orchestra-side state read are both already covered by
upstream integration tests. The point of these tests is to pin down
the **contract** between trigger sites and Unity:

* the helper stays silent unless ``Coordinator/State.onboarding_active``
  is ``True``;
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

ACTIVE_STATE = {"onboarding_active": True, "onboarding_step": None}
INACTIVE_STATE = {"onboarding_active": False, "onboarding_step": None}

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
        first_name="T-W1N",
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
async def test_async_notify_skips_when_onboarding_inactive() -> None:
    """Inactive Coordinators must stay silent — that's the point of the gate."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=INACTIVE_STATE),
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
async def test_async_notify_emits_when_onboarding_active() -> None:
    """Active Coordinators get the event with the canonical payload shape."""
    coordinator = _fake_coordinator(agent_id=42)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
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
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
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
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
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
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
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


def test_sync_notify_posts_inline_when_in_onboarding() -> None:
    """Sync wrapper must POST during the request (no detached daemon thread)."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
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


def test_fire_and_forget_onboarding_event_posts_inline_with_short_timeout() -> None:
    """In-request httpx POST; no Thread.start under CPU throttling."""
    payload = {
        "assistant_id": 1,
        "extra_event_fields": {"subtype": svc.SUBTYPE_INTEGRATION_CONNECTED},
    }
    mock_response = MagicMock()
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(svc, "ADMIN_KEY", "test-key"),
        patch.object(svc, "_adapters_url", return_value="https://adapters.test"),
        patch.object(svc.httpx, "Client", return_value=mock_client) as client_cls,
        patch("threading.Thread") as thread_cls,
    ):
        svc._fire_and_forget_onboarding_event(payload)

    thread_cls.assert_not_called()
    client_cls.assert_called_once_with(timeout=3.0)
    mock_client.post.assert_called_once_with(
        "https://adapters.test/unity/system-event",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    mock_response.raise_for_status.assert_called_once()


def test_fire_and_forget_onboarding_event_swallows_http_errors() -> None:
    """Adapters outages must not propagate to the sync caller."""
    with (
        patch.object(svc, "ADMIN_KEY", "test-key"),
        patch.object(svc, "_adapters_url", return_value="https://adapters.test"),
        patch.object(svc.httpx, "Client", side_effect=RuntimeError("adapters down")),
    ):
        svc._fire_and_forget_onboarding_event(
            {
                "assistant_id": 1,
                "extra_event_fields": {"subtype": svc.SUBTYPE_INTEGRATION_CONNECTED},
            },
        )


@pytest.mark.parametrize(
    "subtype",
    [
        svc.SUBTYPE_INTEGRATION_DEMO_REQUESTED,
        svc.SUBTYPE_INTEGRATION_CONNECT_CHIP_REQUESTED,
        svc.SUBTYPE_INTEGRATION_DEMO_CHIP_REQUESTED,
    ],
)
@pytest.mark.anyio
async def test_integration_onboarding_subtypes_are_registered(subtype: str) -> None:
    """New Integrations events must pass the closed subtype gate."""
    coordinator = _fake_coordinator(agent_id=43)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.notify_coordinator_onboarding_event(
            session=MagicMock(),
            coordinator=coordinator,
            subtype=subtype,
            message="integration event",
            details={"step_id": "integration-read"},
        )
    assert result is True
    assert post.await_args.kwargs["extra_event_fields"] == {
        "subtype": subtype,
        "details": {"step_id": "integration-read", "onboarding": _RENDER},
    }


@pytest.mark.anyio
async def test_step_skipped_event_embeds_step_snapshots() -> None:
    """Skip events tell Unity which step was skipped and what is resolved so far."""
    coordinator = _fake_coordinator(agent_id=15)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
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


@pytest.mark.anyio
async def test_step_unskipped_event_embeds_step_snapshots() -> None:
    """Unskip events mirror skip events so the brain's view cannot diverge."""
    coordinator = _fake_coordinator(agent_id=15)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_step_unskipped_event(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="workspace",
            completed_step_ids=["apps"],
            skipped_step_ids=[],
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_UNSKIPPED,
        "details": {
            "step_id": "workspace",
            "completed_step_ids": ["apps"],
            "skipped_step_ids": [],
            "onboarding": _RENDER,
        },
    }


@pytest.mark.anyio
async def test_step_reset_event_embeds_step_snapshots() -> None:
    """Reset events tell Unity which step reverted and carry the fresh render."""
    coordinator = _fake_coordinator(agent_id=16)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_step_reset_event(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="workspace-mailbox",
            reset_step_ids=["workspace-mailbox"],
            completed_step_ids=["apps"],
            skipped_step_ids=[],
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_RESET,
        "details": {
            "step_id": "workspace-mailbox",
            "reset_step_ids": ["workspace-mailbox"],
            "completed_step_ids": ["apps"],
            "skipped_step_ids": [],
            "onboarding": _RENDER,
        },
    }


@pytest.mark.anyio
async def test_step_completed_event_embeds_progress_and_render() -> None:
    """Completed events name the finished step and carry the fresh render."""
    coordinator = _fake_coordinator(agent_id=17)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_step_completed_event(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="workspace-mailbox",
            completed_step_ids=["apps", "workspace-mailbox"],
            skipped_step_ids=[],
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_COMPLETED,
        "details": {
            "step_id": "workspace-mailbox",
            "completed_step_ids": ["apps", "workspace-mailbox"],
            "skipped_step_ids": [],
            "onboarding": _RENDER,
        },
    }


def test_step_completed_event_safe_sync_fires_without_blocking() -> None:
    """PATCH step completion uses fire-and-forget narration."""
    coordinator = _fake_coordinator(agent_id=17)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_fire_and_forget_onboarding_event") as fire,
    ):
        result = svc.emit_onboarding_step_completed_event_safe_sync(
            session=MagicMock(),
            coordinator=coordinator,
            step_id="workspace-mailbox",
            completed_step_ids=["apps", "workspace-mailbox"],
            skipped_step_ids=[],
        )
    assert result is True
    payload = fire.call_args.args[0]
    assert payload["assistant_id"] == 17
    assert payload["extra_event_fields"] == {
        "subtype": svc.SUBTYPE_ONBOARDING_STEP_COMPLETED,
        "details": {
            "step_id": "workspace-mailbox",
            "completed_step_ids": ["apps", "workspace-mailbox"],
            "skipped_step_ids": [],
            "onboarding": _RENDER,
        },
    }


def test_sync_notify_silent_when_onboarding_inactive() -> None:
    """Gate applies symmetrically across sync + async variants."""
    coordinator = _fake_coordinator()
    with (
        patch.object(svc, "get_coordinator_state", return_value=INACTIVE_STATE),
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


@pytest.mark.anyio
async def test_secret_landed_workspace_narrates_once_on_access_token_create() -> None:
    """The access-token create is the one write that fires the connect nudge."""
    coordinator = _fake_coordinator()
    with patch.object(svc, "maybe_notify_for_assistant_async", new=AsyncMock()) as n:
        await svc.emit_secret_landed_event(
            MagicMock(),
            assistant=coordinator,
            secret_name="GOOGLE_ACCESS_TOKEN",
            is_create=True,
        )
    n.assert_awaited_once()
    assert n.await_args.kwargs["subtype"] == svc.SUBTYPE_WORKSPACE_CONNECTED


@pytest.mark.anyio
async def test_secret_landed_workspace_silent_on_access_token_refresh() -> None:
    """A token refresh updates (not creates) the row, so it must not re-nudge."""
    coordinator = _fake_coordinator()
    with patch.object(svc, "maybe_notify_for_assistant_async", new=AsyncMock()) as n:
        await svc.emit_secret_landed_event(
            MagicMock(),
            assistant=coordinator,
            secret_name="MICROSOFT_ACCESS_TOKEN",
            is_create=False,
        )
    n.assert_not_awaited()


@pytest.mark.anyio
async def test_secret_landed_workspace_silent_on_bundle_secret_create() -> None:
    """Other secrets in the connect bundle (refresh token, scopes, ...) stay quiet."""
    coordinator = _fake_coordinator()
    with patch.object(svc, "maybe_notify_for_assistant_async", new=AsyncMock()) as n:
        await svc.emit_secret_landed_event(
            MagicMock(),
            assistant=coordinator,
            secret_name="GOOGLE_REFRESH_TOKEN",
            is_create=True,
        )
    n.assert_not_awaited()


@pytest.mark.anyio
async def test_secret_landed_integration_narrates_on_every_write() -> None:
    """Integration secrets are unaffected by the workspace-connect de-dupe gate."""
    coordinator = _fake_coordinator()
    with patch.object(svc, "maybe_notify_for_assistant_async", new=AsyncMock()) as n:
        await svc.emit_secret_landed_event(
            MagicMock(),
            assistant=coordinator,
            secret_name="SLACK_BOT_TOKEN",
            is_create=False,
        )
    n.assert_awaited_once()
    assert n.await_args.kwargs["subtype"] == svc.SUBTYPE_INTEGRATION_CONNECTED


@pytest.mark.anyio
async def test_step_started_event_embeds_active_step_snapshot() -> None:
    """Active-step events tell Unity which checklist row the user selected."""
    coordinator = _fake_coordinator(agent_id=16)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
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
    session = MagicMock()
    with (
        patch.object(
            svc,
            "get_coordinator_state",
            return_value={
                "onboarding_active": True,
                "skipped_step_ids": ["phone-number"],
            },
        ),
        patch.object(
            svc,
            "derive_onboarding_progress",
            return_value=["workspace", "apps"],
        ) as derive,
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "set_coordinator_state", MagicMock()) as set_state,
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_session_started_event(
            session=session,
            coordinator=coordinator,
            medium="chat",
        )
    assert result is True
    derive.assert_called_once()
    # The chat pick latches intro_watched and arms the durable chat-intro
    # intent server-side — Console's own PATCH is best-effort redundancy.
    set_state.assert_called_once_with(
        session,
        coordinator=coordinator,
        intro_watched=True,
        pending_chat_intro=True,
    )
    # The state write commits (releasing its advisory lock) before the
    # adapter POST so concurrent state PATCHes never wait on network I/O.
    assert session.commit.called
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields["subtype"] == svc.SUBTYPE_ONBOARDING_SESSION_STARTED
    assert fields["details"] == {
        "medium": "chat",
        "completed_step_ids": ["workspace", "apps"],
        "skipped_step_ids": ["phone-number"],
        "onboarding": _RENDER,
    }


@pytest.mark.anyio
async def test_session_started_call_latches_intro_before_adapter_post() -> None:
    """The call pick latches intro_watched and commits before the POST.

    ``intro_watched`` must never depend solely on Console's concurrent
    state PATCH, and the state write's advisory lock must be released
    (committed) before the adapter network call so concurrent PATCHes
    can't be starved into lock timeouts.
    """
    coordinator = _fake_coordinator(agent_id=13)
    session = MagicMock()
    post = AsyncMock()

    def _commit_before_post() -> None:
        assert post.await_count == 0, "commit must precede the adapter POST"

    session.commit.side_effect = _commit_before_post
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "derive_onboarding_progress", return_value=[]),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "set_coordinator_state", MagicMock()) as set_state,
        patch.object(svc, "_post_unity_system_event", new=post),
    ):
        result = await svc.emit_onboarding_session_started_event(
            session=session,
            coordinator=coordinator,
            medium="call",
        )
    assert result is True
    set_state.assert_called_once_with(
        session,
        coordinator=coordinator,
        intro_watched=True,
        pending_chat_intro=None,
    )
    assert session.commit.called
    assert post.await_count == 1


@pytest.mark.anyio
async def test_session_started_event_omits_empty_step_snapshot() -> None:
    """A fresh workspace produces a compact payload without an empty list."""
    coordinator = _fake_coordinator(agent_id=12)
    with (
        patch.object(svc, "get_coordinator_state", return_value=ACTIVE_STATE),
        patch.object(svc, "derive_onboarding_progress", return_value=[]),
        patch.object(svc, "compute_onboarding_render", return_value=_RENDER),
        patch.object(svc, "set_coordinator_state", MagicMock()),
        patch.object(svc, "_post_unity_system_event", new=AsyncMock()) as post,
    ):
        result = await svc.emit_onboarding_session_started_event(
            session=MagicMock(),
            coordinator=coordinator,
            medium="chat",
        )
    assert result is True
    fields = post.await_args.kwargs["extra_event_fields"]
    assert fields["details"] == {"medium": "chat", "onboarding": _RENDER}
