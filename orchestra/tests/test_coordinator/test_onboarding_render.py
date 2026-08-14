"""Structural invariants of the canonical onboarding graph.

These pin properties that hold regardless of which concrete steps the
onboarding flow contains — trigger/reply pairing consistency and the
``depends_on`` resolution semantics — so they never need touching as the
onboarding steps evolve. Step-specific status/render/ordering behaviour is
validated by incremental manual testing, not by hard-coded assertions.
"""

from __future__ import annotations

from orchestra.services import onboarding_graph as graph


def test_graph_integrity_and_pairing() -> None:
    """Every reference-quiz trigger pairs with a real reply and has mediums."""
    for trigger_id, reply_id in graph.TRIGGER_TO_REPLY.items():
        trigger = graph.STEP_BY_ID[trigger_id]
        assert trigger.kind == "trigger"
        assert trigger.can_skip is True
        assert reply_id in graph.STEP_BY_ID
        assert graph.TRIGGER_TO_OUTBOUND_MEDIUMS[trigger_id]


def test_trigger_poll_scopes_no_duplicate_to_this_conversation() -> None:
    """A re-click after a stale dispatch must produce a fresh clue.

    The poll framing suppresses duplicates only for clues sent in the current
    conversation; a clue from an earlier session is lost from the user's point
    of view, and a categorical no-duplicate rule would strand them polling a
    channel nothing will ever arrive on.
    """
    for trigger_id in graph.TRIGGER_TO_REPLY:
        message = graph.STEP_BY_ID[trigger_id].event.message
        assert "in this conversation" in message
        assert "earlier session is lost" in message
        assert "send a fresh one now" in message


def test_dependencies_satisfied_levels() -> None:
    """ADDRESSED accepts completed-or-skipped; COMPLETED needs completed."""
    assert graph.dependencies_satisfied({}, set(), set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), {"a"}) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), set()) is False
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, set(), {"a"}) is False


def test_workspace_demos_are_settable_triggers_that_never_auto_derive() -> None:
    """Demos are the one trigger class that completes explicitly, not by derivation.

    They must stay out of ``TRIGGER_TO_OUTBOUND_MEDIUMS`` (so
    ``derive_onboarding_progress`` never flips them from a tagged outbound) while
    remaining manually settable, so the assistant can mark them done after the
    full multi-part task.
    """
    for step_id in graph.DEMO_STEP_IDS:
        assert step_id in graph.STEP_BY_ID
        assert graph.STEP_BY_ID[step_id].kind == "trigger"
        assert step_id not in graph.TRIGGER_TO_OUTBOUND_MEDIUMS
        assert graph.manual_completion_block_reason(step_id) is None


def test_integrations_phase_has_three_steps_and_chip_metadata() -> None:
    """The Integrations phase renders connect, read, and action rows."""
    assert graph.phase_step_ids_in_graph_order(graph.PHASE_INTEGRATIONS) == (
        "apps",
        "integration-read",
        "integration-action",
    )
    apps_chips = graph.presentation_for("apps").chips_chat
    assert graph.STEP_BY_ID["apps"].depends_on == {}
    assert [chip.id for chip in apps_chips] == [
        "day-to-day-tools",
        "crm-sales",
        "dev-ops",
    ]
    assert apps_chips[0].metadata == {
        "gallery_category": "productivity",
        "search_query": "productivity|calendar|docs",
    }
    assert graph.STEP_BY_ID["integration-read"].event is not None
    assert graph.STEP_BY_ID["integration-read"].event.subtype == (
        "integration_demo_requested"
    )
    assert graph.STEP_BY_ID["integration-action"].depends_on == {
        "integration-read": graph.ADDRESSED,
    }


def test_integration_chip_events_resolve_server_side() -> None:
    """Integrations chips use the same graph-owned chip resolver as Tasks."""
    connect = graph.chip_event_for("apps", "crm-sales")
    assert connect is not None
    assert connect.subtype == "integration_connect_chip_requested"
    assert connect.details["gallery_category"] == "crm_sales"
    assert connect.details["search_query"] == "crm|sales|hubspot|pipedrive"

    demo = graph.chip_event_for("integration-read", "connected-app-brief")
    assert demo is not None
    assert demo.subtype == "integration_demo_chip_requested"
    assert demo.details["trigger_step_id"] == "integration-read"
    assert demo.details["instruction"] == (
        "Pull the latest from one of my connected apps and brief me here"
    )


def test_manual_completion_block_reason() -> None:
    """Communication and auto-triggered rows reject manual completion.

    Explicitly-completed beats (workspace demos, discord-connect, learning) must
    be settable; schedule rows with durable checks also fall through as allowed.
    """
    assert graph.manual_completion_block_reason("email-reference") is not None
    assert graph.manual_completion_block_reason("email-reply") is not None
    assert graph.manual_completion_block_reason("workspace-mailbox") is None
    assert graph.manual_completion_block_reason("workspace-drive") is None
    assert graph.manual_completion_block_reason("workspace-calendar") is None
    assert graph.manual_completion_block_reason("learn-from-correction") is None
    assert graph.manual_completion_block_reason("my-computer-demo") is None
    assert graph.manual_completion_block_reason("integration-read") is None
    assert graph.manual_completion_block_reason("integration-action") is None
    assert graph.manual_completion_block_reason("apps") is None
    assert graph.manual_completion_block_reason("create-scheduled-task") is None
    # Teams rows live in the Workspace phase now but keep the auto-derived
    # completion of the other channel steps — Twin must never tick them by hand.
    assert graph.manual_completion_block_reason("ms-teams-connect") is not None
    assert graph.manual_completion_block_reason("ms-teams-reference") is not None
    assert graph.manual_completion_block_reason("ms-teams-message") is not None


def test_ms_teams_steps_are_microsoft_only_workspace_steps() -> None:
    """The Teams rows render under Workspace and only for a Microsoft workspace.

    They were relocated from Communication; visibility is gated on the connected
    provider so a Google workspace (or none connected) never sees them.
    """
    teams_ids = ("ms-teams-connect", "ms-teams-reference", "ms-teams-message")
    for step_id in teams_ids:
        step = graph.STEP_BY_ID[step_id]
        assert step.phase == graph.PHASE_WORKSPACE
        assert step.providers == ("microsoft",)
        assert graph.step_visible_for_provider(step, "microsoft") is True
        assert graph.step_visible_for_provider(step, "google") is False
        assert graph.step_visible_for_provider(step, None) is False

    # No Teams step is left behind in the Communication phase.
    assert not [
        step_id
        for step_id in teams_ids
        if graph.STEP_BY_ID[step_id].phase == graph.PHASE_COMMUNICATION
    ]

    # They sit at the tail of the Workspace phase, after the demos.
    workspace_ids = graph.phase_step_ids_in_graph_order(graph.PHASE_WORKSPACE)
    assert workspace_ids[-3:] == teams_ids
