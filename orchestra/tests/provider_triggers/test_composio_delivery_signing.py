"""Golden signing vector for cross-repo Composio delivery helpers."""

from __future__ import annotations

from orchestra.provider_triggers.composio_trigger_adapter import (
    sign_composio_webhook_headers,
)
from orchestra.tests.provider_triggers.composio_delivery import (
    serialize_composio_payload,
    sign_composio_payload,
)


def test_sign_composio_payload_matches_production_helper() -> None:
    """Pin the digest shape mirrored in unity/tests/provider_trigger_delivery.py."""

    raw_body = serialize_composio_payload(
        {"id": "evt_signing_vector", "type": "github.issue_created"},
    )
    kwargs = {
        "signing_secret": "test-composio-webhook-secret",
        "webhook_id": "wh_vector_1",
        "timestamp": "1700000000",
    }
    assert sign_composio_payload(raw_body, **kwargs) == sign_composio_webhook_headers(
        raw_body=raw_body,
        **kwargs,
    )
