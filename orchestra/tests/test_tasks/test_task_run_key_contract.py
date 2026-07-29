"""The projected run key must match the one Unify builds for the same occurrence.

Create-or-adopt converges only when both sides produce the same key. Orchestra's
builder claimed to match Unify's while applying neither of its normalizers, so a
team destination serialized as ``team:11`` against ``team-11`` and a due time as
``2026-07-29T16:50:00+00:00`` against ``20260729T165000Z``. Every occurrence got
two execution rows, the second read as a concurrent peer, and the overlap guard
skipped every tick of a ten-minute campaign runtime — silently, for days.
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
