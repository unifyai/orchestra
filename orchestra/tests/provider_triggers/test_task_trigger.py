"""Authored task trigger union contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from orchestra.provider_triggers.task_trigger import (
    CommunicationTrigger,
    ProviderEventTrigger,
    parse_task_trigger,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "task_trigger_contract"
)


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_parse_task_trigger_coerces_legacy_medium_only_row() -> None:
    trigger = parse_task_trigger(_load_fixture("task_trigger.communication.legacy.json"))
    assert isinstance(trigger, CommunicationTrigger)
    assert trigger.kind == "communication"
    assert trigger.medium == "email"


def test_parse_task_trigger_accepts_provider_event_kind() -> None:
    trigger = parse_task_trigger(_load_fixture("task_trigger.provider_event.v1.json"))
    assert isinstance(trigger, ProviderEventTrigger)
    assert trigger.event_slug == "github.issue_created"
