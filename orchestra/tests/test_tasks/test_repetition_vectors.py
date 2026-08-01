"""Golden regression corpus for the recurrence arithmetic.

This was a cross-repo drift guard: the arithmetic existed twice, here and in
``unify/task_scheduler/types/repetition.py``, and both repos checked a
byte-identical corpus so a semantic change ported to only one side failed.

Successor projection is now owned solely by Orchestra — unify no longer
computes slots, and its copy is deleted — so the corpus guards this one
implementation against accidental change instead. The vectors were verified
identical across both implementations at the point the duplicate was removed,
so nothing about the semantics moved with the ownership.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from orchestra.services.task_repetition import (
    RepeatPattern,
    deterministic_jitter_seconds,
    next_repeated_start_at,
)

_VECTORS = json.loads(
    Path(__file__)
    .with_name("repeat_projection_vectors.json")
    .read_text(
        encoding="utf-8",
    ),
)


@pytest.mark.parametrize(
    "case",
    _VECTORS["next_occurrence"],
    ids=lambda case: case["name"],
)
def test_next_occurrence_matches_shared_vector(case: dict) -> None:
    result = next_repeated_start_at(
        previous_start=datetime.fromisoformat(case["previous_start"]),
        patterns=[RepeatPattern.model_validate(p) for p in case["patterns"]],
        current_occurrence_index=case.get("current_occurrence_index", 0),
        now=datetime.fromisoformat(case["now"]),
    )
    assert (result.isoformat() if result else None) == case["expected"]


@pytest.mark.parametrize(
    "case",
    _VECTORS["dispatch_jitter"],
    ids=lambda case: case["name"],
)
def test_dispatch_jitter_matches_shared_vector(case: dict) -> None:
    offset = deterministic_jitter_seconds(
        task_id=case["task_id"],
        slot=datetime.fromisoformat(case["slot"]),
        patterns=[RepeatPattern.model_validate(p) for p in case["patterns"]],
    )
    assert offset == case["expected"]
