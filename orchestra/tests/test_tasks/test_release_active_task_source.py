"""Unit tests for release_active_task_source crash/retry writeback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestra.services.task_machine_state_service import release_active_task_source


def test_release_active_task_source_fail_mode():
    row = SimpleNamespace(
        id=555,
        data={"status": "active", "task_id": 9, "instance_id": 1},
        key_order={},
    )
    session = MagicMock()
    session.query.return_value.filter.return_value.one_or_none.return_value = row

    with patch(
        "orchestra.services.task_machine_state_service._replace_log_payload",
    ) as replace:
        result = release_active_task_source(
            session,
            project_id=1,
            source_task_log_id=555,
            mode="fail",
            info="worker gone",
        )

    assert result["updated"] is True
    assert result["status_before"] == "active"
    assert result["status_after"] == "failed"
    replace.assert_called_once()
    written = replace.call_args.args[1]
    assert written["status"] == "failed"
    assert "worker gone" in str(written["info"])


def test_release_active_task_source_reopen_scheduled():
    row = SimpleNamespace(
        id=555,
        data={
            "status": "active",
            "task_id": 9,
            "instance_id": 1,
            "schedule": {"start_at": "2026-07-20T10:00:00+00:00"},
            "repeat": {"kind": "weekly"},
        },
        key_order={},
    )
    session = MagicMock()
    session.query.return_value.filter.return_value.one_or_none.return_value = row

    with patch(
        "orchestra.services.task_machine_state_service._replace_log_payload",
    ) as replace:
        result = release_active_task_source(
            session,
            project_id=1,
            source_task_log_id=555,
            mode="reopen",
        )

    assert result["updated"] is True
    assert result["status_after"] == "scheduled"
    assert replace.call_args.args[1]["status"] == "scheduled"


def test_release_active_task_source_noop_when_not_active():
    row = SimpleNamespace(
        id=555,
        data={"status": "failed", "task_id": 9},
        key_order={},
    )
    session = MagicMock()
    session.query.return_value.filter.return_value.one_or_none.return_value = row

    result = release_active_task_source(
        session,
        project_id=1,
        source_task_log_id=555,
        mode="fail",
    )

    assert result["updated"] is False
    assert result["reason"] == "not_active"


def test_release_active_task_source_rejects_bad_mode():
    session = MagicMock()
    with pytest.raises(ValueError, match="fail"):
        release_active_task_source(
            session,
            project_id=1,
            source_task_log_id=555,
            mode="explode",
        )
