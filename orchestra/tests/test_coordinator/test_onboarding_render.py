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


def test_dependencies_satisfied_levels() -> None:
    """ADDRESSED accepts completed-or-skipped; COMPLETED needs completed."""
    assert graph.dependencies_satisfied({}, set(), set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), {"a"}) is True
    assert graph.dependencies_satisfied({"a": graph.ADDRESSED}, set(), set()) is False
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, {"a"}, set()) is True
    assert graph.dependencies_satisfied({"a": graph.COMPLETED}, set(), {"a"}) is False


def test_manual_completion_block_reason() -> None:
    """Communication and auto-triggered rows reject manual completion.

    Workspace demos are the deliberate exception: they are multi-part tasks the
    assistant finishes and then marks done explicitly, so they must be settable.
    """
    assert graph.manual_completion_block_reason("email-reference") is not None
    assert graph.manual_completion_block_reason("email-reply") is not None
    assert graph.manual_completion_block_reason("workspace-mailbox") is None
    assert graph.manual_completion_block_reason("workspace-drive") is None
    assert graph.manual_completion_block_reason("workspace-calendar") is None
    assert graph.manual_completion_block_reason("apps") is None
    assert graph.manual_completion_block_reason("create-scheduled-task") is None
