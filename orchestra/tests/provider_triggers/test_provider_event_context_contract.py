"""Provider-event context request/response contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.web.api.log.task_machine_schema import (
    ProviderEventContextRequest,
    ProviderEventContextResponse,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "task_trigger_contract"
)


def test_provider_event_context_request_v1_forbids_extra_fields() -> None:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_context_request.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    request = ProviderEventContextRequest.model_validate(payload)
    assert request.audience == EVENT_CONTEXT_AUDIENCE
    assert request.event_context_ref == payload["event_context_ref"]

    with pytest.raises(ValueError):
        ProviderEventContextRequest.model_validate({**payload, "raw_body": "secret"})


def test_provider_event_context_response_v1_matches_shared_fixture() -> None:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_context_response.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    response = ProviderEventContextResponse.model_validate(payload)
    assert response.receipt_id == payload["receipt_id"]
    assert response.source_body == payload["source_body"]
    assert response.expires_at is not None
