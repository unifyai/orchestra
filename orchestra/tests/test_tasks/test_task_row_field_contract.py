"""Contract tests for task row field vocabulary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.services.task_row_field import (
    AuthoredTaskField,
    RuntimeTaskField,
    RuntimeTaskStatus,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "task_trigger_contract"
    / "task_row_field_contract.v1.json"
)


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def test_authored_task_field_matches_shared_fixture() -> None:
    fixture = _load_fixture()
    assert sorted(AuthoredTaskField.values()) == sorted(fixture["authored_fields"])


def test_runtime_task_field_matches_shared_fixture() -> None:
    fixture = _load_fixture()
    assert sorted(RuntimeTaskField.values()) == sorted(fixture["runtime_fields"])


def test_runtime_task_status_matches_shared_fixture() -> None:
    fixture = _load_fixture()
    assert sorted(RuntimeTaskStatus.values()) == sorted(
        fixture["runtime_status_values"],
    )


def test_classify_provider_event_update_fields_uses_enum_partitions() -> None:
    from orchestra.services.task_mutation_contract import (
        ProviderEventWriteRejected,
        classify_provider_event_update_fields,
    )
    from orchestra.services.task_row_field import ProviderEventUpdateKind

    existing = {
        "trigger": {
            "kind": "provider_event",
            "state": "enabled",
            "connection_id": "conn-1",
            "backend_id": "composio",
            "canonical_app_slug": "github",
            "event_slug": "github.issue_created",
            "schema_version": "1",
            "filters": [],
        },
    }
    assert (
        classify_provider_event_update_fields(
            {"description": "updated"},
            existing_data=existing,
        )
        is ProviderEventUpdateKind.authored
    )
    assert (
        classify_provider_event_update_fields(
            {"status": "active", "activated_by": "explicit"},
            existing_data=existing,
        )
        is ProviderEventUpdateKind.runtime
    )
    with pytest.raises(ProviderEventWriteRejected):
        classify_provider_event_update_fields(
            {"description": "mixed", "status": "active"},
            existing_data=existing,
        )
    assert (
        classify_provider_event_update_fields(
            {"status": "triggerable"},
            existing_data=existing,
        )
        is ProviderEventUpdateKind.authored
    )
