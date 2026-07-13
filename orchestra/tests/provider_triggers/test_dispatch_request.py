"""Provider-event dispatch envelope contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.provider_triggers.dispatch_request import ProviderEventDispatchRequest

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "task_trigger_contract"
)


def test_dispatch_request_v1_forbids_extra_and_rejects_raw_payload() -> None:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_dispatch_request.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    request = ProviderEventDispatchRequest.model_validate(payload)
    assert request.contract_version == "1"
    assert "raw_body" not in request.model_dump()

    with pytest.raises(ValueError):
        ProviderEventDispatchRequest.model_validate({**payload, "raw_body": "secret"})


def test_dispatch_request_fixture_parity_with_unity_and_deploy() -> None:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_dispatch_request.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    request = ProviderEventDispatchRequest.model_validate(payload)
    assert request.operation_id == payload["operation_id"]
    assert request.audience == payload["audience"]
