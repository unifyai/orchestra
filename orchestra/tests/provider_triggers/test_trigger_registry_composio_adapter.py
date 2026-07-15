"""Curated registry and Composio trigger adapter contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest

from orchestra.provider_triggers.composio_trigger_adapter import (
    ComposioTriggerAdapter,
    verify_composio_signature,
)
from orchestra.provider_triggers.resource_resolution import (
    resolve_resource_id_from_filters,
)
from orchestra.provider_triggers.trigger_adapter import TriggerProvisionRequest
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.provider_triggers.trigger_matching import matches_filters
from orchestra.provider_triggers.trigger_projectors import project_curated_payload
from orchestra.provider_triggers.trigger_registry import (
    GITHUB_ISSUE_CREATED,
    list_trigger_catalog_payloads,
    require_canonical_trigger_event,
    validate_authored_filters,
)
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.web.api.tasks.schema import TriggerCatalogResponse
from orchestra.web.api.tasks.views import get_task_trigger_catalog

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "provider_trigger_contract"
)


def _load_composio_fixture() -> dict[str, Any]:
    return json.loads(
        (FIXTURE_DIR / "composio_github_issue_created.redacted.json").read_text(
            encoding="utf-8",
        ),
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_composio_retry_deliveries_normalize_to_same_identity_and_projection() -> None:
    payload = _load_composio_fixture()
    adapter = ComposioTriggerAdapter(api_key="test-key", webhook_secret="secret")
    first = adapter.normalize_delivery(
        headers={"webhook-id": "msg_retry_1"},
        raw_body=payload,
    )
    second = adapter.normalize_delivery(
        headers={"webhook-id": "msg_retry_2"},
        raw_body=json.dumps(payload).encode("utf-8"),
    )

    assert first.provider_event_identity == "evt_provider_trigger_example"
    assert second.provider_event_identity == first.provider_event_identity
    assert first.curated_projection == second.curated_projection
    assert first.provider_trigger_slug == "GITHUB_ISSUE_CREATED_TRIGGER"
    assert first.envelope["event_slug"] == GITHUB_ISSUE_CREATED
    assert adapter.stable_event_identity(payload) == first.provider_event_identity


def test_github_issue_created_filters_are_deterministic_on_curated_projection() -> None:
    payload = _load_composio_fixture()
    event = require_canonical_trigger_event(GITHUB_ISSUE_CREATED)
    projection = project_curated_payload(
        projector_key=event.projector_key,
        payload=payload,
    )
    assert projection == {
        "repository": "octocat/hello-world",
        "author": "octocat",
        "labels": ["bug"],
        "title": "provider trigger fixture",
    }

    nested = {
        "action": "opened",
        "issue": {
            "title": "Café Bug",
            "user": {"login": "OctoCat"},
            "labels": [{"name": "Bug"}],
            "repository": {"full_name": "OctoCat/Hello-World"},
        },
    }
    nested_projection = project_curated_payload(
        projector_key=event.projector_key,
        payload=nested,
    )
    assert nested_projection["repository"] == "octocat/hello-world"
    assert nested_projection["author"] == "octocat"
    assert nested_projection["labels"] == ["bug"]
    assert nested_projection["title"] == "café bug"

    assert matches_filters(
        projection=projection,
        filters=[
            {"field": "repository", "operator": "is", "value": "OCTOCAT/Hello-World"},
            {"field": "author", "operator": "is", "value": "octocat"},
            {"field": "labels", "operator": "includes", "value": "bug"},
            {"field": "title", "operator": "contains", "value": "provider"},
        ],
        event=event,
    )
    assert not matches_filters(
        projection=projection,
        filters=[{"field": "labels", "operator": "excludes", "value": "bug"}],
        event=event,
    )
    assert not matches_filters(
        projection={"repository": None, "author": None, "labels": None, "title": None},
        filters=[{"field": "author", "operator": "is not", "value": "octocat"}],
        event=event,
    )

    adapter = ComposioTriggerAdapter(api_key="test-key")
    delivery = adapter.normalize_delivery(headers={}, raw_body=payload)
    assert adapter.delivery_matches_filters(
        delivery,
        [{"field": "repository", "operator": "is", "value": "octocat/Hello-World"}],
        event_slug=GITHUB_ISSUE_CREATED,
    )


def test_unknown_source_fields_remain_in_source_body_and_cannot_match() -> None:
    payload = _load_composio_fixture()
    event = require_canonical_trigger_event(GITHUB_ISSUE_CREATED)
    payload = {
        **payload,
        "data": {
            **payload["data"],
            "priority": "critical",
            "full_name": "decoy/should-not-match",
        },
    }
    adapter = ComposioTriggerAdapter(api_key="test-key")
    delivery = adapter.normalize_delivery(headers={}, raw_body=payload)

    assert set(delivery.curated_projection) <= {
        "repository",
        "author",
        "labels",
        "title",
    }
    assert delivery.source_body["data"]["priority"] == "critical"
    assert delivery.source_body["data"]["full_name"] == "decoy/should-not-match"
    assert not matches_filters(
        projection=delivery.curated_projection,
        filters=[
            {
                "field": "priority",
                "operator": "is",
                "value": "critical",
            },
        ],
        event=event,
    )
    assert not adapter.delivery_matches_filters(
        delivery,
        [{"field": "repository", "operator": "is", "value": "decoy/should-not-match"}],
        event_slug=GITHUB_ISSUE_CREATED,
    )


def test_composio_provision_and_delivery_authorization_fail_closed() -> None:
    calls: list[tuple[str, str]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, url))
        if method == "GET" and "connected_accounts/ca_inactive" in url:
            return _FakeResponse(
                payload={"status": "EXPIRED", "user_id": "assistant:1"},
            )
        if method == "GET" and "connected_accounts/" in url:
            return _FakeResponse(payload={"status": "ACTIVE", "user_id": "assistant:1"})
        if method == "POST" and url.endswith("/trigger_instances"):
            return _FakeResponse(
                status_code=200,
                payload={"id": "ti_created_1", "trigger_id": "ti_created_1"},
            )
        return _FakeResponse(status_code=500, text="unexpected")

    adapter = ComposioTriggerAdapter(api_key="test-key", request_fn=request_fn)

    with pytest.raises(PermissionError, match="repository_inaccessible"):
        adapter.provision(
            TriggerProvisionRequest(
                connection_id="conn-1",
                provider_connection_id="ca_inactive",
                provider_user_id="assistant:1",
                event_slug=GITHUB_ISSUE_CREATED,
                schema_version="1",
                canonical_app_slug="github",
                callback_url="https://example.test/hooks",
                idempotency_key="idem-1",
                ingress_key="ingress-1",
                resource_id="octocat/hello-world",
            ),
        )

    with pytest.raises(ValueError, match="repository resource"):
        adapter.provision(
            TriggerProvisionRequest(
                connection_id="conn-1",
                provider_connection_id="ca_active",
                provider_user_id="assistant:1",
                event_slug=GITHUB_ISSUE_CREATED,
                schema_version="1",
                canonical_app_slug="github",
                callback_url="https://example.test/hooks",
                idempotency_key="idem-2",
                ingress_key="ingress-2",
                resource_id="",
                filters=[],
            ),
        )

    result = adapter.provision(
        TriggerProvisionRequest(
            connection_id="conn-1",
            provider_connection_id="ca_active",
            provider_user_id="assistant:1",
            event_slug=GITHUB_ISSUE_CREATED,
            schema_version="1",
            canonical_app_slug="github",
            callback_url="https://example.test/hooks",
            idempotency_key="idem-3",
            ingress_key="ingress-3",
            resource_id="octocat/Hello-World",
        ),
    )
    assert result.external_trigger_id == "ti_created_1"
    assert result.signing_secret_ref == "env:COMPOSIO_WEBHOOK_SECRET"

    delivery = adapter.normalize_delivery(headers={}, raw_body=_load_composio_fixture())
    assert (
        adapter.authorize_delivery(
            delivery=delivery,
            expected_connected_account_id="ca_other",
            expected_external_trigger_id="ti_provider_trigger_example",
            expected_provider_user_id="assistant:provider-trigger-probe",
            expected_resource_id="octocat/hello-world",
        )
        == "connected_account_mismatch"
    )
    assert (
        adapter.authorize_delivery(
            delivery=delivery,
            expected_connected_account_id="ca_provider_trigger_example",
            expected_external_trigger_id="ti_other",
            expected_provider_user_id="assistant:provider-trigger-probe",
            expected_resource_id="octocat/hello-world",
        )
        == "subscription_mismatch"
    )
    assert (
        adapter.authorize_delivery(
            delivery=delivery,
            expected_connected_account_id="ca_provider_trigger_example",
            expected_external_trigger_id="ti_provider_trigger_example",
            expected_provider_user_id="assistant:other",
            expected_resource_id="octocat/hello-world",
        )
        == "provider_user_mismatch"
    )
    assert (
        adapter.authorize_delivery(
            delivery=delivery,
            expected_connected_account_id="ca_provider_trigger_example",
            expected_external_trigger_id="ti_provider_trigger_example",
            expected_provider_user_id="assistant:provider-trigger-probe",
            expected_resource_id="octocat/other",
        )
        == "resource_mismatch"
    )
    assert (
        adapter.authorize_delivery(
            delivery=delivery,
            expected_connected_account_id="ca_provider_trigger_example",
            expected_external_trigger_id="ti_provider_trigger_example",
            expected_provider_user_id="assistant:provider-trigger-probe",
            expected_resource_id="octocat/Hello-World",
        )
        is None
    )
    missing_fields = adapter.normalize_delivery(
        headers={},
        raw_body={
            **_load_composio_fixture(),
            "metadata": {
                "trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
            },
            "data": {"title": "x"},
        },
    )
    assert (
        adapter.authorize_delivery(
            delivery=missing_fields,
            expected_connected_account_id="ca_provider_trigger_example",
            expected_external_trigger_id="ti_provider_trigger_example",
            expected_provider_user_id="assistant:provider-trigger-probe",
            expected_resource_id="octocat/hello-world",
        )
        == "connected_account_mismatch"
    )
    assert (
        resolve_resource_id_from_filters(
            require_canonical_trigger_event(GITHUB_ISSUE_CREATED),
            [{"field": "repository", "operator": "is", "value": "OctoCat/Hello-World"}],
        )
        == "octocat/hello-world"
    )
    assert ("POST", f"{adapter.base_url}/trigger_instances") in calls


def test_task_trigger_catalog_exposes_only_curated_versioned_github_issue_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = list_trigger_catalog_payloads()
    assert [item["event_slug"] for item in payloads] == [GITHUB_ISSUE_CREATED]
    event = payloads[0]
    assert event["schema_version"] == "1"
    assert event["resource_kind"] == "github_repository"
    assert event["resource_id_format"] == "owner/name"
    assert event["backends"] == ["composio", "pipedream"]
    operators_by_field: dict[str, set[str]] = {}
    for item in event["filters"]:
        operators_by_field.setdefault(item["field"], set()).add(item["operator"])
    assert operators_by_field["repository"] == {"is", "is not", "is any of"}
    assert operators_by_field["author"] == {"is", "is not", "is any of"}
    assert operators_by_field["labels"] == {"includes", "excludes"}
    assert operators_by_field["title"] == {"contains", "does not contain"}
    assert "contains" not in operators_by_field["labels"]

    # Endpoint takes a FastAPI Depends session; call with an explicit None and a
    # healthy topology stub so this unit test does not need a live DB session.
    stub_healthy_provider_trigger_topology(monkeypatch)
    response = get_task_trigger_catalog(session=None)
    catalog = TriggerCatalogResponse.model_validate(response.info.model_dump())
    assert catalog.available is True
    assert len(catalog.events) == 1
    assert catalog.events[0].event_slug == GITHUB_ISSUE_CREATED
    assert catalog.events[0].backends == ["composio", "pipedream"]

    mapping_backends = {
        mapping.backend_id
        for mapping in require_canonical_trigger_event(
            GITHUB_ISSUE_CREATED,
        ).provider_mappings
    }
    assert "pipedream" in mapping_backends

    assert get_trigger_provider_adapter("composio").backend_id == "composio"
    assert get_trigger_provider_adapter("pipedream").backend_id == "pipedream"


def test_validate_authored_filters_rejects_uncurated_fields_and_operators() -> None:
    event = require_canonical_trigger_event(GITHUB_ISSUE_CREATED)
    validated = validate_authored_filters(
        event,
        [{"field": "repository", "operator": "is", "value": "octocat/hello-world"}],
    )
    assert validated == [
        {
            "field": "repository",
            "operator": "is",
            "value": "octocat/hello-world",
        },
    ]
    with pytest.raises(ValueError, match="Unsupported filter field"):
        validate_authored_filters(
            event,
            [{"field": "data.priority", "operator": "is", "value": "high"}],
        )
    with pytest.raises(ValueError, match="not valid for field"):
        validate_authored_filters(
            event,
            [{"field": "labels", "operator": "contains", "value": "bug"}],
        )
    with pytest.raises(ValueError, match="Filter value is required"):
        validate_authored_filters(
            event,
            [{"field": "labels", "operator": "includes", "value": []}],
        )


def test_verify_composio_signature_accepts_v1_hmac_over_id_timestamp_body() -> None:
    raw_body = b'{"id":"evt_1"}'
    webhook_id = "msg_1"
    timestamp = str(int(time.time()))
    secret = "composio-webhook-secret"
    digest = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            f"{webhook_id}.{timestamp}.{raw_body.decode()}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    assert verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=timestamp,
        signature_header=f"v1,{digest}",
    )
    assert not verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=timestamp,
        signature_header="v1,deadbeef",
    )
    assert not verify_composio_signature(
        signing_secret=secret,
        raw_body=raw_body,
        webhook_id=webhook_id,
        webhook_timestamp=str(int(time.time()) - 10_000),
        signature_header=f"v1,{digest}",
    )

    adapter = ComposioTriggerAdapter(api_key="test-key", webhook_secret=secret)
    assert adapter.verify_delivery(
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{digest}",
        },
        raw_body=raw_body,
        signing_secrets=[],
    )
