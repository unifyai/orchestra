"""Unit tests for the canonical onboarding graph + render computation.

These exercise the pure ``depends_on`` semantics and the
``compute_onboarding_render`` status/next-target logic against mocks,
without the FastAPI stack or a live DB. They pin the contract both Unity
brains and the Console checklist now rely on: statuses are server-
computed, communication trigger rows complete from assistant outbound
evidence, and the valid next targets carry ready-to-use nudge copy.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestra.services import coordinator_service as svc
from orchestra.services import onboarding_graph as graph


def _fake_coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=1,
        user_id="user-1",
        organization_id=None,
        is_coordinator=True,
    )


def _statuses(render: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in render["steps"]}


def _next_ids(render: dict) -> list[str]:
    return [target["id"] for target in render["next_targets"]]


def test_graph_integrity_and_pairing() -> None:
    """The graph imports cleanly and exposes a consistent trigger pairing."""
    # Import already ran ``_assert_graph_integrity`` without raising.
    assert len(graph.ONBOARDING_GRAPH) == 30
    assert len(graph.TRIGGER_TO_REPLY) == 7
    # 7 reference-quiz triggers + 3 workspace demo triggers all derive from
    # assistant outbound evidence.
    assert len(graph.TRIGGER_TO_OUTBOUND_MEDIUMS) == 10
    # Every reference-quiz trigger points at a real reply step.
    for trigger_id, reply_id in graph.TRIGGER_TO_REPLY.items():
        trigger = graph.STEP_BY_ID[trigger_id]
        assert trigger.kind == "trigger"
        assert trigger.can_skip is True
        assert reply_id in graph.STEP_BY_ID
        assert graph.TRIGGER_TO_OUTBOUND_MEDIUMS[trigger_id]


def test_workspace_demo_trigger_contract() -> None:
    """Workspace demos are reply-less triggers that derive from a unify_message."""
    demo_ids = ("workspace-mailbox", "workspace-drive", "workspace-calendar")
    for step_id in demo_ids:
        step = graph.STEP_BY_ID[step_id]
        assert step.kind == "trigger"
        assert step.phase == graph.PHASE_WORKSPACE
        assert step.can_skip is True
        assert step.derivable is False
        # No paired reply: completion is the assistant's delivered summary.
        assert step.paired_reply is None
        assert step_id not in graph.TRIGGER_TO_REPLY
        assert graph.TRIGGER_TO_OUTBOUND_MEDIUMS[step_id] == ("unify_message",)
        assert step.depends_on == {"workspace": graph.COMPLETED}
        interaction = step.event.details["interaction"]
        assert interaction["type"] == "workspace_demo"
        assert interaction["channel"] == step.channel
        assert interaction["instructions"]


def test_dependencies_satisfied_levels() -> None:
    """ADDRESSED accepts completed-or-skipped; COMPLETED needs completed."""
    assert graph.dependencies_satisfied({}, set(), set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), {"a"}) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), set()) is False
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, set(), {"a"}) is False


def _render_with(
    completed: list[str],
    skipped: list[str],
    active: str | None,
    skipped_phases: list[str] | None = None,
    local_mode: bool = True,
) -> dict:
    with (
        patch.object(svc, "derive_onboarding_progress", return_value=list(completed)),
        patch.object(
            svc,
            "get_coordinator_state",
            return_value={
                "mode": "onboarding",
                "onboarding_step": active,
                "skipped_step_ids": list(skipped),
                "skipped_phase_ids": list(skipped_phases or []),
            },
        ),
    ):
        return svc.compute_onboarding_render(
            MagicMock(),
            coordinator=_fake_coordinator(),
            local_mode=local_mode,
        )


def test_render_fresh_start_exposes_each_section_head() -> None:
    """With nothing done, the first step in each independent section is a target."""
    render = _render_with(completed=[], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "available"
    assert statuses["email-reply"] == "locked"
    assert statuses["whatsapp-number"] == "available"
    assert statuses["whatsapp-message-reference"] == "locked"
    assert statuses["whatsapp-call-reference"] == "locked"
    assert statuses["phone-number"] == "available"
    assert statuses["sms-reference"] == "locked"
    assert statuses["phone-call-reference"] == "locked"
    assert statuses["slack-connect"] == "available"
    assert statuses["slack-reference"] == "locked"
    assert statuses["discord-connect"] == "available"
    assert statuses["discord-reference"] == "locked"
    assert statuses["workspace"] == "available"
    assert statuses["apps"] == "locked"
    assert statuses["schedule"] == "available"
    assert statuses["learning-coming-soon"] == "coming_soon"
    assert statuses["canvas-coming-soon"] == "coming_soon"
    assert statuses["my-computer-coming-soon"] == "coming_soon"
    assert statuses["your-computer-coming-soon"] == "coming_soon"
    assert statuses["teams-coming-soon"] == "coming_soon"
    assert statuses["hiring-coming-soon"] == "coming_soon"
    steps = {step["id"]: step for step in render["steps"]}
    assert steps["email-reference"]["can_skip"] is True
    assert steps["email-reply"]["dependencies"] == [
        {
            "id": "email-reference",
            "title": "Trigger email from T-W1N",
            "status": "available",
            "resolution": "completed",
            "satisfied": False,
        },
    ]
    assert steps["sms-reference"]["dependencies"] == [
        {
            "id": "phone-number",
            "title": "Add your phone number",
            "status": "available",
            "resolution": "completed",
            "satisfied": False,
        },
    ]
    assert steps["schedule"]["dependencies"] == []
    assert steps["slack-reference"]["title"] == "Trigger Slack message from T-W1N"
    assert steps["slack-message"]["title"] == "Reply to Slack message"
    assert steps["discord-reference"]["title"] == "Trigger Discord message from T-W1N"
    assert steps["discord-message"]["title"] == "Reply to Discord message"
    assert _next_ids(render) == [
        "email-reference",
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "workspace",
        "schedule",
    ]
    # Every next target carries spoken + chat nudge copy.
    for target in render["next_targets"]:
        assert target["nudge_voice"]
        assert target["nudge_chat"]


def test_next_target_nudges_are_checklist_row_first() -> None:
    """Startable non-reply steps point users at the checklist row first."""
    render = _render_with(completed=[], skipped=[], active=None)
    targets = {target["id"]: target for target in render["next_targets"]}
    for step_id in (
        "email-reference",
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "workspace",
        "schedule",
    ):
        target = targets[step_id]
        assert "row" in target["nudge_chat"]
        assert "Onboarding checklist" in target["nudge_chat"]

    apps_render = _render_with(completed=["workspace"], skipped=[], active=None)
    apps_target = {target["id"]: target for target in apps_render["next_targets"]}[
        "apps"
    ]
    assert "row" in apps_target["nudge_chat"]
    assert "Onboarding checklist" in apps_target["nudge_chat"]


def test_setup_flow_notes_start_with_row_clicks() -> None:
    """Flow notes explain what clicking the row opens or triggers."""
    render = _render_with(completed=[], skipped=[], active=None)
    steps = {step["id"]: step for step in render["steps"]}
    for step_id in (
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "workspace",
        "apps",
        "schedule",
    ):
        assert steps[step_id]["flow_note"].startswith("Clicking the '")


def test_render_reply_done_does_not_complete_trigger() -> None:
    """Inbound reply completion does not prove Twin sent the trigger outbound."""
    render = _render_with(completed=["email-reply"], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "available"
    assert statuses["email-reply"] == "done"
    assert statuses["whatsapp-number"] == "available"
    assert _next_ids(render) == [
        "email-reference",
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "workspace",
        "schedule",
    ]


def test_render_active_reply_does_not_complete_trigger() -> None:
    """The active reply step is a resume pointer, not outbound evidence."""
    render = _render_with(completed=[], skipped=[], active="email-reply")
    statuses = _statuses(render)
    assert statuses["email-reference"] == "available"
    assert statuses["email-reply"] == "locked"


def test_render_outbound_trigger_done_unlocks_reply() -> None:
    """A trigger completes once outbound transcript evidence is derived."""
    render = _render_with(completed=["email-reference"], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "done"
    assert statuses["email-reply"] == "available"


def test_render_workspace_demos_lock_until_workspace_connected() -> None:
    """Demo rows stay locked until workspace completes, then become targets."""
    fresh = _statuses(_render_with(completed=[], skipped=[], active=None))
    assert fresh["workspace-mailbox"] == "locked"
    assert fresh["workspace-drive"] == "locked"
    assert fresh["workspace-calendar"] == "locked"

    connected = _render_with(completed=["workspace"], skipped=[], active=None)
    statuses = _statuses(connected)
    assert statuses["workspace"] == "done"
    assert statuses["workspace-mailbox"] == "available"
    assert statuses["workspace-drive"] == "available"
    assert statuses["workspace-calendar"] == "available"
    next_ids = _next_ids(connected)
    assert "workspace-mailbox" in next_ids
    assert "workspace-drive" in next_ids
    assert "workspace-calendar" in next_ids


def test_render_workspace_demo_done_from_outbound() -> None:
    """A delivered demo summary renders the demo row as done."""
    render = _render_with(
        completed=["workspace", "workspace-mailbox"],
        skipped=[],
        active=None,
    )
    statuses = _statuses(render)
    assert statuses["workspace-mailbox"] == "done"
    assert statuses["workspace-drive"] == "available"


def test_render_skipped_completed_dependency_cascades_to_dependents() -> None:
    """A skipped completed-only prerequisite renders dependent rows as skipped."""
    render = _render_with(completed=[], skipped=["phone-number"], active=None)
    statuses = _statuses(render)
    assert statuses["phone-number"] == "skipped"
    assert statuses["sms-reference"] == "skipped"
    assert statuses["sms-message"] == "skipped"
    assert statuses["phone-call-reference"] == "skipped"
    assert statuses["phone-call"] == "skipped"


def test_completed_dependency_skip_cascade() -> None:
    """Skipping a setup step cascades to descendants that require completion."""
    assert graph.completion_coupled_steps("phone-number") == (
        "phone-number",
        "sms-reference",
        "sms-message",
        "phone-call-reference",
        "phone-call",
    )
    assert graph.completion_coupled_steps("sms-message") == (
        "phone-number",
        "sms-reference",
        "sms-message",
        "phone-call-reference",
        "phone-call",
    )


def test_render_skipped_phase_suppresses_next_targets_without_skipping_steps() -> None:
    """A section-level defer is separate from per-step skip status."""
    render = _render_with(
        completed=[],
        skipped=[],
        active=None,
        skipped_phases=[graph.PHASE_WORKSPACE],
    )
    statuses = _statuses(render)
    assert statuses["workspace"] == "available"
    assert statuses["apps"] == "locked"
    assert render["skipped_phase_ids"] == [graph.PHASE_WORKSPACE]
    assert _next_ids(render) == [
        "email-reference",
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "schedule",
    ]


# ---------------------------------------------------------------------------
# Presentation copy carried on the render
# ---------------------------------------------------------------------------


def test_render_carries_phase_headers_and_step_presentation() -> None:
    """The render carries phase headers + per-step copy so Console renders
    straight from it without its own duplicated presentation map."""
    render = _render_with(completed=[], skipped=[], active=None)
    assert [p["title"] for p in render["phases"]] == [
        "Communication",
        "Workspace",
        "Integrations",
        "Tasks",
        "Learning",
        "Canvas",
        "My Computer",
        "Your Computer",
        "Teams",
        "Hiring",
    ]
    comms = next(p for p in render["phases"] if p["id"] == "communication")
    assert comms["title"] == "Communication"
    assert comms["phase"] == graph.PHASE_COMMUNICATION
    assert "reference quiz" in comms["framing"]
    steps = {s["id"]: s for s in render["steps"]}
    assert steps["email-reference"]["description"]
    assert steps["email-reference"]["estimated_time"]
    assert steps["email-reference"]["kind"] == "trigger"
    assert steps["email-reference"]["paired_reply"] == "email-reply"
    assert steps["email-reference"]["nudge_chat"]
    assert "reference quiz" in steps["email-reference"]["flow_note"]
    interaction = steps["email-reference"]["interaction"]
    assert interaction["type"] == "reference_quiz"
    assert interaction["channel"] == "email"
    assert interaction["tool_name"] == "send_email"
    # Clues are invented by the model at runtime, never hard-coded in the graph.
    assert "quote" not in interaction
    assert "answer" not in interaction
    assert "accepted_answers" not in interaction
    schedule = steps["schedule"]
    assert [c["id"] for c in schedule["chips_chat"]]
    # Non-chip steps carry empty chip lists rather than omitting the field.
    assert steps["workspace"]["chips_chat"] == []
    assert steps["learning-coming-soon"]["title"] == "[Coming soon]"
    assert steps["learning-coming-soon"]["status"] == "coming_soon"


def test_catalog_carries_step_contract_and_interactions() -> None:
    """The static catalog exposes the same graph-owned step contract as render."""
    catalog = svc.build_onboarding_catalog(local_mode=True)
    steps = {step["id"]: step for step in catalog["steps"]}
    email_reference = steps["email-reference"]

    assert email_reference["kind"] == "trigger"
    assert email_reference["paired_reply"] == "email-reply"
    assert email_reference["nudge_voice"]
    assert email_reference["phase_id"] == "communication"
    assert email_reference["interaction"]["type"] == "reference_quiz"
    assert (
        email_reference["event"]["details"]["interaction"]["type"] == "reference_quiz"
    )


# ---------------------------------------------------------------------------
# Deployment gating
# ---------------------------------------------------------------------------


def test_render_hosted_keeps_onboarding_catalog() -> None:
    """Hosted and self-host deployments share the Coordinator onboarding catalog."""
    render = _render_with(completed=[], skipped=[], active=None, local_mode=False)
    assert [p["id"] for p in render["phases"]] == [
        "communication",
        "workspace",
        "integrations",
        "tasks",
        "learning",
        "canvas",
        "my-computer",
        "your-computer",
        "teams",
        "hiring",
    ]
    assert "email-reference" in {s["id"] for s in render["steps"]}
    assert "my-computer-coming-soon" in {s["id"] for s in render["steps"]}


def test_render_local_keeps_all_phases() -> None:
    """A local self-host install keeps every phase visible."""
    render = _render_with(completed=[], skipped=[], active=None, local_mode=True)
    assert len(render["phases"]) == 10


def test_phase_visibility_helper() -> None:
    assert graph.phase_is_visible(graph.PHASE_WORKSPACE, local_mode=False) is True
    assert graph.phase_is_visible(graph.PHASE_COMMUNICATION, local_mode=False) is True
    assert graph.phase_is_visible(graph.PHASE_MY_COMPUTER, local_mode=True) is True


# ---------------------------------------------------------------------------
# Static catalog
# ---------------------------------------------------------------------------


def test_catalog_local_lists_all_phases_with_copy() -> None:
    catalog = svc.build_onboarding_catalog(local_mode=True)
    assert len(catalog["phases"]) == 10
    assert len(catalog["steps"]) == len(graph.ONBOARDING_GRAPH)
    schedule = next(s for s in catalog["steps"] if s["id"] == "schedule")
    assert schedule["description"]
    assert [c["id"] for c in schedule["chips_chat"]]


def test_catalog_hosted_keeps_onboarding_catalog() -> None:
    catalog = svc.build_onboarding_catalog(local_mode=False)
    assert len(catalog["phases"]) == 10
    assert "learning-coming-soon" in {s["id"] for s in catalog["steps"]}


def test_onboarding_local_mode_signal() -> None:
    """Only hosted staging/production resolve to non-local; self-host and
    every non-hosted environment (dev, CI, tests) are local mode."""
    cases = [
        (SimpleNamespace(is_self_host=True, environment="production"), True),
        (SimpleNamespace(is_self_host=True, environment="staging"), True),
        (SimpleNamespace(is_self_host=False, environment="dev"), True),
        (SimpleNamespace(is_self_host=False, environment="test"), True),
        (SimpleNamespace(is_self_host=False, environment="staging"), False),
        (SimpleNamespace(is_self_host=False, environment="production"), False),
    ]
    for fake_settings, expected in cases:
        with patch.object(svc, "settings", fake_settings):
            assert svc.onboarding_local_mode() is expected


# ---------------------------------------------------------------------------
# Voice intro briefing
# ---------------------------------------------------------------------------


def test_voice_intro_briefing_fresh_start() -> None:
    """A fresh-start render yields the deadpan first-call intro, the first
    valid next target's voice nudge, and the pause escape hatch."""
    render = _render_with(completed=[], skipped=[], active=None)
    briefing = svc.compose_voice_intro_briefing(render)

    assert "Hi, I'm T dash W 1 N." in briefing
    assert "deadpan corporate-training satire" in briefing
    assert "tongue-in-cheek meta joke" in briefing
    assert "opening bit only" in briefing
    assert "normal helpful onboarding" in briefing
    assert 'I\'m not a "tool". I\'m not an "agent".' in briefing
    assert "Don't think about prompting me, or configuring me" in briefing
    assert "Krispy Kreme" not in briefing
    assert "voice static" not in briefing
    assert "really annoying music" not in briefing
    # First valid next target is the email reference quiz; its voice nudge is
    # surfaced verbatim after the intro as the concrete next step.
    primary = render["next_targets"][0]
    assert primary["id"] == "email-reference"
    assert primary["nudge_voice"] in briefing
    assert graph.COMMUNICATION_FRAMING not in briefing
    # Pause escape hatch + interruption guidance.
    assert "pause onboarding" in briefing.lower()
    assert "interrupt" in briefing.lower()


def test_voice_intro_briefing_tolerates_empty_render() -> None:
    """With no phases/targets the briefing still returns the orientation frame
    without a next-step or framing line, and never raises."""
    briefing = svc.compose_voice_intro_briefing({})

    assert "Hi, I'm T dash W 1 N." in briefing
    assert "concrete next step" not in briefing
    assert graph.COMMUNICATION_FRAMING not in briefing
