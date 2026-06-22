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
    assert len(graph.ONBOARDING_GRAPH) == 22
    assert len(graph.TRIGGER_TO_REPLY) == 7
    # Every trigger points at a real reply step.
    for trigger_id, reply_id in graph.TRIGGER_TO_REPLY.items():
        assert graph.STEP_BY_ID[trigger_id].kind == "trigger"
        assert reply_id in graph.STEP_BY_ID


def test_dependencies_satisfied_levels() -> None:
    """ADDRESSED accepts completed-or-skipped; COMPLETED needs completed."""
    assert graph.dependencies_satisfied({}, set(), set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), {"a"}) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), set()) is False
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, set(), {"a"}) is False


def _render_with(completed: list[str], skipped: list[str], active: str | None) -> dict:
    with (
        patch.object(svc, "derive_onboarding_progress", return_value=list(completed)),
        patch.object(
            svc,
            "get_coordinator_state",
            return_value={
                "mode": "onboarding",
                "onboarding_step": active,
                "skipped_step_ids": list(skipped),
            },
        ),
    ):
        return svc.compute_onboarding_render(
            MagicMock(),
            coordinator=_fake_coordinator(),
        )


def test_render_fresh_start_exposes_each_section_head() -> None:
    """With nothing done, the first step in each independent section is a target."""
    render = _render_with(completed=[], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "available"
    assert statuses["email-reply"] == "locked"
    assert statuses["workspace"] == "available"
    assert statuses["apps"] == "locked"
    assert statuses["act"] == "available"
    assert statuses["schedule"] == "locked"
    assert _next_ids(render) == ["email-reference", "workspace", "act"]
    # Every next target carries spoken + chat nudge copy.
    for target in render["next_targets"]:
        assert target["nudge_voice"]
        assert target["nudge_chat"]


def test_render_reply_done_infers_trigger_and_unlocks_next() -> None:
    """A completed reply marks its trigger done and opens the next step."""
    render = _render_with(completed=["email-reply"], skipped=[], active=None)
    statuses = _statuses(render)
    assert statuses["email-reference"] == "done"  # inferred from the reply
    assert statuses["email-reply"] == "done"
    assert statuses["whatsapp-number"] == "available"
    assert _next_ids(render) == ["whatsapp-number", "workspace", "act"]


def test_render_active_reply_infers_trigger_done() -> None:
    """The active reply step counts its trigger as already sent."""
    render = _render_with(completed=[], skipped=[], active="email-reply")
    statuses = _statuses(render)
    assert statuses["email-reference"] == "done"
    assert statuses["email-reply"] == "available"


def test_render_skipped_dependency_unlocks_addressed_dependent() -> None:
    """An ADDRESSED edge opens once its dependency is skipped (not just done)."""
    # Skipping a reply still resolves its trigger and unlocks downstream.
    render = _render_with(completed=[], skipped=["email-reply"], active=None)
    statuses = _statuses(render)
    assert statuses["email-reply"] == "skipped"
    assert statuses["email-reference"] == "skipped"
    assert statuses["whatsapp-number"] == "available"
