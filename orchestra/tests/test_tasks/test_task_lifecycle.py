"""The lifecycle projection treats a held run like any other terminal run."""

from __future__ import annotations

import pytest

from orchestra.services import task_lifecycle
from orchestra.services.task_lifecycle import Lifecycle, derive_task_lifecycle


@pytest.mark.parametrize(
    "states, expected",
    [
        ({"held"}, Lifecycle.completed),
        ({"completed"}, Lifecycle.completed),
        ({"failed"}, Lifecycle.completed),
        ({"cancelled"}, Lifecycle.completed),
        ({"scheduled"}, Lifecycle.disarmed),
        (set(), Lifecycle.disarmed),
    ],
)
def test_disarmed_one_shot_is_completed_after_any_terminal_run(
    monkeypatch,
    states,
    expected,
) -> None:
    monkeypatch.setattr(
        task_lifecycle,
        "_execution_states",
        lambda *a, **k: set(states),
    )
    lifecycle = derive_task_lifecycle(
        None,
        project_id=1,
        source_task_log_id=7,
        data={"enabled": False},
    )
    assert lifecycle == expected


def test_running_outranks_a_held_sibling(monkeypatch) -> None:
    monkeypatch.setattr(
        task_lifecycle,
        "_execution_states",
        lambda *a, **k: {"held", "running"},
    )
    assert (
        derive_task_lifecycle(
            None,
            project_id=1,
            source_task_log_id=7,
            data={"enabled": False},
        )
        == Lifecycle.running
    )
