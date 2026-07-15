"""Curated, versioned provider-event trigger registry.

The registry is the single Orchestra-owned catalog for canonical events,
filter fields/operators, normalization rules, and provider backend mappings.
Dynamic provider catalogs are not authoritative for authored trigger intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from orchestra.provider_triggers.filter_operators import FilterOperator

GITHUB_ISSUE_CREATED = "github.issue_created"
GITHUB_APP_SLUG = "github"
SYNTHETIC_ITEM_CREATED = "synthetic.item_created"
SYNTHETIC_APP_SLUG = "synthetic"
REGISTRY_SCHEMA_VERSION_V1 = "1"
COMPOSIO_BACKEND_ID = "composio"
PIPEDREAM_BACKEND_ID = "pipedream"
LOCAL_BACKEND_ID = "local"
COMPOSIO_GITHUB_ISSUE_CREATED_SLUG = "GITHUB_ISSUE_CREATED_TRIGGER"
PIPEDREAM_GITHUB_ISSUE_COMPONENT = "github-new-or-updated-issue"
CATALOG_VISIBILITY_PUBLIC = "public"
CATALOG_VISIBILITY_CONFORMANCE_ONLY = "conformance_only"


@dataclass(frozen=True)
class FilterFieldDefinition:
    """One curated filterable field for a canonical event."""

    field: str
    value_type: str
    operators: tuple[FilterOperator, ...]
    description: str
    normalization: str = "string"


@dataclass(frozen=True)
class ProviderEventMapping:
    """Provider-specific mapping for one canonical registry event."""

    backend_id: str
    provider_trigger_slug: str
    retry_stable_identity_field: str
    signature_headers: tuple[str, ...]
    signature_scheme: str
    timestamp_tolerance_seconds: int
    catalog_status: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalTriggerEvent:
    """One versioned canonical provider-event definition."""

    event_slug: str
    canonical_app_slug: str
    schema_version: str
    filters: tuple[FilterFieldDefinition, ...]
    provider_mappings: tuple[ProviderEventMapping, ...]
    projector_key: str
    resource_kind: str
    resource_id_format: str
    resource_filter_field: str
    resource_filter_operator: str
    selection_contract: str
    projection_fields: tuple[str, ...] = ()
    catalog_visibility: str = CATALOG_VISIBILITY_PUBLIC

    def mapping_for(self, backend_id: str) -> ProviderEventMapping | None:
        """Return the provider mapping for one backend, if curated."""

        for mapping in self.provider_mappings:
            if mapping.backend_id == backend_id:
                return mapping
        return None

    def filter_definition(self, field_name: str) -> FilterFieldDefinition | None:
        """Return the filter definition for one curated field name."""

        for definition in self.filters:
            if definition.field == field_name:
                return definition
        return None

    def catalog_payload(
        self,
        *,
        available_backends: set[str] | None = None,
    ) -> dict[str, Any]:
        """Serialize this event for the typed Tasks catalog response.

        When ``available_backends`` is provided, only those backends are listed
        even if additional curated mappings exist for later tickets.
        """

        filters: list[dict[str, Any]] = []
        for definition in self.filters:
            for operator in definition.operators:
                filters.append(
                    {
                        "field": definition.field,
                        "operator": operator.value,
                        "value_type": definition.value_type,
                    },
                )
        backends = [mapping.backend_id for mapping in self.provider_mappings]
        if available_backends is not None:
            backends = [
                backend for backend in backends if backend in available_backends
            ]
        return {
            "event_slug": self.event_slug,
            "canonical_app_slug": self.canonical_app_slug,
            "schema_version": self.schema_version,
            "filters": filters,
            "backends": backends,
            "resource_kind": self.resource_kind,
            "resource_id_format": self.resource_id_format,
            "resource_filter_field": self.resource_filter_field,
            "resource_filter_operator": self.resource_filter_operator,
            "selection_contract": self.selection_contract,
        }


_GITHUB_ISSUE_CREATED_V1 = CanonicalTriggerEvent(
    event_slug=GITHUB_ISSUE_CREATED,
    canonical_app_slug=GITHUB_APP_SLUG,
    schema_version=REGISTRY_SCHEMA_VERSION_V1,
    projector_key=GITHUB_ISSUE_CREATED,
    resource_kind="github_repository",
    resource_id_format="owner/name",
    resource_filter_field="repository",
    resource_filter_operator="is",
    selection_contract=(
        "Provide the exact repository as a repository filter using operator "
        "'is' and value 'owner/name'. v1 does not browse the full GitHub "
        "catalog; provisioning validates access for that repository through "
        "the selected connection."
    ),
    projection_fields=("repository", "author", "labels", "title"),
    filters=(
        FilterFieldDefinition(
            field="repository",
            value_type="string",
            operators=(
                FilterOperator.is_,
                FilterOperator.is_not,
                FilterOperator.is_any_of,
            ),
            description="Canonical owner/name repository path.",
            normalization="repository",
        ),
        FilterFieldDefinition(
            field="author",
            value_type="string",
            operators=(
                FilterOperator.is_,
                FilterOperator.is_not,
                FilterOperator.is_any_of,
            ),
            description="Issue author login.",
            normalization="author",
        ),
        FilterFieldDefinition(
            field="labels",
            value_type="string_array",
            operators=(FilterOperator.includes, FilterOperator.excludes),
            description="Exact issue label names.",
            normalization="labels",
        ),
        FilterFieldDefinition(
            field="title",
            value_type="string",
            operators=(
                FilterOperator.contains,
                FilterOperator.does_not_contain,
            ),
            description="Issue title text.",
            normalization="title",
        ),
    ),
    provider_mappings=(
        ProviderEventMapping(
            backend_id=COMPOSIO_BACKEND_ID,
            provider_trigger_slug=COMPOSIO_GITHUB_ISSUE_CREATED_SLUG,
            retry_stable_identity_field="id",
            signature_headers=(
                "webhook-id",
                "webhook-timestamp",
                "webhook-signature",
            ),
            signature_scheme="composio_v3_hmac_sha256",
            timestamp_tolerance_seconds=300,
            catalog_status="included",
            notes=(
                "Composio catalog slug is GITHUB_ISSUE_CREATED_TRIGGER.",
                "V3 deliveries expose a top-level id as the retry-stable identity.",
            ),
        ),
        ProviderEventMapping(
            backend_id=PIPEDREAM_BACKEND_ID,
            provider_trigger_slug=PIPEDREAM_GITHUB_ISSUE_COMPONENT,
            retry_stable_identity_field="trace_id",
            signature_headers=("x-pd-signature",),
            signature_scheme="pipedream_hmac_sha256_timestamp_dot_body",
            timestamp_tolerance_seconds=300,
            catalog_status="included_with_opened_filter",
            notes=(
                "Pipedream exposes github-new-or-updated-issue rather than "
                "issue-created-only; projection treats action=opened as create.",
            ),
        ),
    ),
)

_SYNTHETIC_ITEM_CREATED_V1 = CanonicalTriggerEvent(
    event_slug=SYNTHETIC_ITEM_CREATED,
    canonical_app_slug=SYNTHETIC_APP_SLUG,
    schema_version=REGISTRY_SCHEMA_VERSION_V1,
    projector_key=SYNTHETIC_ITEM_CREATED,
    resource_kind="synthetic_item",
    resource_id_format="item_id",
    resource_filter_field="item_id",
    resource_filter_operator="is",
    selection_contract=(
        "Provide the exact item id as an item_id filter using operator 'is'."
    ),
    projection_fields=("item_id", "title"),
    catalog_visibility=CATALOG_VISIBILITY_CONFORMANCE_ONLY,
    filters=(
        FilterFieldDefinition(
            field="item_id",
            value_type="string",
            operators=(FilterOperator.is_, FilterOperator.is_not),
            description="Synthetic item identifier.",
            normalization="item_id",
        ),
        FilterFieldDefinition(
            field="title",
            value_type="string",
            operators=(
                FilterOperator.contains,
                FilterOperator.does_not_contain,
            ),
            description="Synthetic item title.",
            normalization="title",
        ),
    ),
    provider_mappings=(
        ProviderEventMapping(
            backend_id=LOCAL_BACKEND_ID,
            provider_trigger_slug="synthetic-item-created",
            retry_stable_identity_field="delivery_id",
            signature_headers=("x-synthetic-signature",),
            signature_scheme="synthetic_hmac_sha256",
            timestamp_tolerance_seconds=300,
            catalog_status="conformance_only",
            notes=("Conformance-only mapping for registry/engine validation.",),
        ),
    ),
)

_REGISTRY: dict[tuple[str, str], CanonicalTriggerEvent] = {
    (GITHUB_ISSUE_CREATED, REGISTRY_SCHEMA_VERSION_V1): _GITHUB_ISSUE_CREATED_V1,
    (SYNTHETIC_ITEM_CREATED, REGISTRY_SCHEMA_VERSION_V1): _SYNTHETIC_ITEM_CREATED_V1,
}


def get_canonical_trigger_event(
    event_slug: str,
    *,
    schema_version: str = REGISTRY_SCHEMA_VERSION_V1,
) -> CanonicalTriggerEvent | None:
    """Return one curated event definition, or None when unsupported."""

    return _REGISTRY.get((event_slug, schema_version))


def require_canonical_trigger_event(
    event_slug: str,
    *,
    schema_version: str = REGISTRY_SCHEMA_VERSION_V1,
) -> CanonicalTriggerEvent:
    """Return one curated event definition or raise for unsupported mappings."""

    event = get_canonical_trigger_event(event_slug, schema_version=schema_version)
    if event is None:
        raise LookupError(
            f"Unsupported trigger event {event_slug!r} schema {schema_version!r}",
        )
    return event


def list_canonical_trigger_events(
    *,
    include_conformance_only: bool = False,
) -> list[CanonicalTriggerEvent]:
    """Return curated trigger events in stable catalog order."""

    events = sorted(
        _REGISTRY.values(),
        key=lambda event: (event.event_slug, event.schema_version),
    )
    if include_conformance_only:
        return events
    return [
        event
        for event in events
        if event.catalog_visibility == CATALOG_VISIBILITY_PUBLIC
    ]


def list_trigger_catalog_payloads(
    *,
    available_backends: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return catalog payloads for the typed Tasks catalog route."""

    if available_backends is None:
        from orchestra.provider_triggers.trigger_adapter_registry import (
            TRIGGER_PROVIDER_ADAPTERS,
        )

        available_backends = set(TRIGGER_PROVIDER_ADAPTERS)
    return [
        event.catalog_payload(available_backends=available_backends)
        for event in list_canonical_trigger_events()
    ]


def resolve_provider_mapping(
    *,
    backend_id: str,
    event_slug: str,
    schema_version: str = REGISTRY_SCHEMA_VERSION_V1,
) -> ProviderEventMapping:
    """Resolve a curated provider mapping or fail closed."""

    event = require_canonical_trigger_event(
        event_slug,
        schema_version=schema_version,
    )
    mapping = event.mapping_for(backend_id)
    if mapping is None:
        raise LookupError(
            f"No curated mapping for backend {backend_id!r} on {event_slug!r}",
        )
    return mapping


def validate_authored_filters(
    event: CanonicalTriggerEvent,
    filters: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Validate authored filters against curated field/operator definitions.

    Unknown fields, unsupported operators, and arbitrary JSON paths are rejected.
    """

    if not filters:
        return []
    validated: list[dict[str, Any]] = []
    for item in filters:
        field_name = str(item.get("field", "")).strip()
        operator_name = str(item.get("operator", "")).strip()
        definition = event.filter_definition(field_name)
        if definition is None:
            raise ValueError(f"Unsupported filter field {field_name!r}")
        try:
            operator = FilterOperator(operator_name)
        except ValueError as exc:
            raise ValueError(f"Unsupported filter operator {operator_name!r}") from exc
        if operator not in definition.operators:
            raise ValueError(
                f"Operator {operator.value!r} is not valid for field {field_name!r}",
            )
        value = item.get("value")
        if value is None or value == "" or value == []:
            raise ValueError(
                f"Filter value is required for field {definition.field!r}",
            )
        if definition.value_type == "string_array" and isinstance(value, list):
            if any(not str(entry).strip() for entry in value):
                raise ValueError(
                    f"Filter value is required for field {definition.field!r}",
                )
        validated.append(
            {
                "field": definition.field,
                "operator": operator.value,
                "value": value,
            },
        )
    return validated


def curated_provider_event_filters(
    trigger_event_slug: str,
    schema_version: str,
    filters: list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Validate curated filters then return the hash-stable normalized list."""

    from orchestra.provider_triggers.activation_revision import (
        normalize_provider_event_filters,
    )

    event = require_canonical_trigger_event(
        trigger_event_slug,
        schema_version=schema_version,
    )
    validated = validate_authored_filters(event, filters)
    return normalize_provider_event_filters(validated)
