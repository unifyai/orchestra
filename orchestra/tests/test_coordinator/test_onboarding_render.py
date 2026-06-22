"""Unit tests for the canonical onboarding graph + render computation.

These exercise the pure ``depends_on`` semantics and the
``compute_onboarding_render`` status/next-target logic against mocks,
without the FastAPI stack or a live DB. They pin the contract both Droid
brains and the Console checklist now rely on: statuses are server-
computed, trigger rows are inferred from their paired reply, and the
valid next targets carry ready-to-use nudge copy.
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
    assert len(graph.ONBOARDING_GRAPH) == 27
    assert len(graph.TRIGGER_TO_REPLY) == 7
    # Every trigger points at a real reply step.
    for trigger_id, reply_id in graph.TRIGGER_TO_REPLY.items():
        trigger = graph.STEP_BY_ID[trigger_id]
        assert trigger.kind == "trigger"
        assert trigger.can_skip is True
        assert reply_id in graph.STEP_BY_ID


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
            "title": "Email the first reference",
            "status": "available",
            "resolution": "addressed",
            "satisfied": False,
        },
    ]
    assert steps["sms-reference"]["dependencies"] == [
        {
            "id": "phone-number",
            "title": "Add your phone number",
            "status": "available",
            "resolution": "addressed",
            "satisfied": False,
        },
    ]
    assert steps["schedule"]["dependencies"] == []
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


def test_render_reply_done_infers_trigger_and_unlocks_next() -> None:
    """A completed reply marks its trigger done without gating other media."""
    render = _render_with(completed=["email-reply"], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "done"  # inferred from the reply
    assert statuses["email-reply"] == "done"
    assert statuses["whatsapp-number"] == "available"
    assert _next_ids(render) == [
        "whatsapp-number",
        "phone-number",
        "slack-connect",
        "discord-connect",
        "workspace",
        "schedule",
    ]


def test_render_active_reply_infers_trigger_done() -> None:
    """The active reply step counts its trigger as already sent."""
    render = _render_with(completed=[], skipped=[], active="email-reply")
    statuses = _statuses(render)
    assert statuses["email-reference"] == "done"
    assert statuses["email-reply"] == "available"


def test_render_skipped_dependency_unlocks_addressed_dependent() -> None:
    """An ADDRESSED edge opens once its dependency is skipped (not just done)."""
    render = _render_with(completed=[], skipped=["phone-number"], active=None)
    statuses = _statuses(render)
    assert statuses["phone-number"] == "skipped"
    assert statuses["sms-reference"] == "available"
    assert statuses["phone-call-reference"] == "available"


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
    steps = {s["id"]: s for s in render["steps"]}
    assert steps["email-reference"]["description"]
    assert steps["email-reference"]["estimated_time"]
    schedule = steps["schedule"]
    assert [c["id"] for c in schedule["chips_chat"]]
    # Non-chip steps carry empty chip lists rather than omitting the field.
    assert steps["workspace"]["chips_chat"] == []
    assert steps["learning-coming-soon"]["title"] == "[Coming soon]"
    assert steps["learning-coming-soon"]["status"] == "coming_soon"


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
