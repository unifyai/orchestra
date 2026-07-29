"""The projected run key must match the one Unify builds for the same occurrence.

Create-or-adopt converges only when both sides produce the same key. Orchestra's
builder claimed to match Unify's while applying neither of its normalizers, so a
team destination serialized as ``team:11`` against ``team-11`` and a due time as
``2026-07-29T16:50:00+00:00`` against ``20260729T165000Z``. Every occurrence got
two execution rows, the second read as a concurrent peer, and the overlap guard
skipped every tick of a ten-minute campaign runtime — silently, for days.

The trigger lane converges the same way. A dispatcher waking on a projected
trigger row with no remembered event provenance rebuilds the key from that row
alone, so the medium the projection carries belongs in the tail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.services.task_machine_state_service import _build_open_execution_run_key

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "task_trigger_contract"
    / "task_run_key_contract.v1.json"
)


def _cases() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_run_key_matches_shared_contract(case: dict) -> None:
    assert _build_open_execution_run_key(**case["inputs"]) == case["run_key"]


def test_free_form_components_are_normalized() -> None:
    """Anything that reaches the key is lowercased and hyphen-separated."""

    key = _build_open_execution_run_key(
        delivery="offline",
        wake="scheduled",
        assistant_id="1406",
        destination="Team:11",
        task_id=12,
        revision="r1",
        due_at="2026-07-29T16:50:00+00:00",
    )
    assert ":team-11:" in key


def test_triggered_wake_has_no_tail_of_its_own() -> None:
    """A projected trigger row Unify cannot name is a second row per occurrence.

    This tail was ``arm`` while Unify built ``once`` from the same row, so the
    dispatcher adopted nothing and minted its own execution — the scheduled-lane
    defect, unfixed, on the trigger lane.
    """

    key = _build_open_execution_run_key(
        delivery="live",
        wake="triggered",
        assistant_id="42",
        destination=None,
        task_id=301,
        revision="rev-trigger",
    )
    assert key.endswith(":once")


def test_only_a_triggered_wake_takes_the_medium() -> None:
    """Unify reads a medium as provenance on a triggered wake and nowhere else."""

    inputs = dict(
        delivery="offline",
        assistant_id="1406",
        destination="team:11",
        task_id=12,
        revision="r1",
        due_at="2026-07-29T16:50:00+00:00",
    )
    assert _build_open_execution_run_key(
        wake="scheduled",
        trigger_medium="sms_message",
        **inputs,
    ) == _build_open_execution_run_key(wake="scheduled", **inputs)
