"""Live provider trigger catalog and identity probes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.provider_triggers.provider_identity import (
    composio_v3_event_identity,
    pipedream_delivery_identity,
)
from orchestra.tests.provider_triggers.provider_probe_runner import (
    probe_composio_github_issue_created,
    probe_pipedream_github_issue_created,
    provider_probes_available,
    verify_pipedream_signature,
)

pytestmark = pytest.mark.skipif(
    not provider_probes_available(),
    reason="staging provider secrets are not available locally",
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "provider_trigger_contract"
)


def test_provider_trigger_contract_probe_records_composio_identity_and_catalog() -> (
    None
):
    result = probe_composio_github_issue_created()
    assert result.catalog_status == "included"
    assert result.provider_trigger_slug == "GITHUB_ISSUE_CREATED_TRIGGER"
    assert result.retry_stable_identity_field == "id"
    assert (
        composio_v3_event_identity(result.fixture_payload)
        == "evt_provider_trigger_example"
    )
    assert "webhook-signature" in result.signature_headers

    fixture_path = FIXTURE_DIR / "composio_github_issue_created.redacted.json"
    on_disk = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert on_disk["metadata"]["trigger_slug"] == result.provider_trigger_slug


def test_provider_trigger_contract_probe_records_pipedream_identity_and_catalog() -> (
    None
):
    result = probe_pipedream_github_issue_created()
    assert result.catalog_status == "included_with_opened_filter"
    assert result.provider_trigger_slug == "github-new-or-updated-issue"
    assert result.retry_stable_identity_field == "trace_id"
    assert (
        pipedream_delivery_identity({}, result.fixture_payload)
        == "pd_trace_provider_trigger_example"
    )

    signing_key = "provider-trigger-signing-key"
    raw_body = json.dumps(result.fixture_payload, sort_keys=True).encode("utf-8")
    import hashlib
    import hmac
    import time

    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    header = f"t={timestamp},v1={digest}"
    assert verify_pipedream_signature(
        signing_key=signing_key,
        raw_body=raw_body,
        signature_header=header,
    )

    fixture_path = FIXTURE_DIR / "pipedream_github_issue.redacted.json"
    on_disk = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert on_disk["trace_id"] == result.fixture_payload["trace_id"]
