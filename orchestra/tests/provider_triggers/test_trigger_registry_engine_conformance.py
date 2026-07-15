"""Conformance tests for provider-neutral registry and matching engine."""

from __future__ import annotations

from orchestra.provider_triggers.resource_resolution import (
    resolve_resource_id_from_filters,
)
from orchestra.provider_triggers.trigger_matching import matches_filters
from orchestra.provider_triggers.trigger_projectors import project_curated_payload
from orchestra.provider_triggers.trigger_registry import (
    GITHUB_ISSUE_CREATED,
    SYNTHETIC_ITEM_CREATED,
    list_canonical_trigger_events,
    list_trigger_catalog_payloads,
    require_canonical_trigger_event,
)


def test_public_catalog_excludes_conformance_only_events() -> None:
    payloads = list_trigger_catalog_payloads()
    assert [item["event_slug"] for item in payloads] == [GITHUB_ISSUE_CREATED]
    assert payloads[0]["resource_kind"] == "github_repository"
    assert payloads[0]["resource_id_format"] == "owner/name"
    assert payloads[0]["selection_contract"]

    all_events = list_canonical_trigger_events(include_conformance_only=True)
    assert {event.event_slug for event in all_events} == {
        GITHUB_ISSUE_CREATED,
        SYNTHETIC_ITEM_CREATED,
    }


def test_synthetic_event_loads_without_shared_path_slug_branches() -> None:
    event = require_canonical_trigger_event(SYNTHETIC_ITEM_CREATED)
    assert event.resource_kind == "synthetic_item"
    assert event.resource_filter_field == "item_id"

    payload = {"data": {"item_id": "ITEM-42", "title": "Hello Synthetic"}}
    projection = project_curated_payload(
        projector_key=event.projector_key,
        payload=payload,
    )
    assert projection == {
        "item_id": "item-42",
        "title": "hello synthetic",
    }

    filters = [{"field": "item_id", "operator": "is", "value": "ITEM-42"}]
    assert (
        matches_filters(
            projection=projection,
            filters=filters,
            event=event,
        )
        is True
    )
    assert (
        matches_filters(
            projection=projection,
            filters=[{"field": "item_id", "operator": "is", "value": "other"}],
            event=event,
        )
        is False
    )

    resource_id = resolve_resource_id_from_filters(event, filters)
    assert resource_id == "item-42"

    catalog = event.catalog_payload(available_backends={"local"})
    assert catalog["resource_kind"] == "synthetic_item"
    assert catalog["backends"] == ["local"]
