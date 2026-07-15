"""Registry-owned resource resolution for provider-event triggers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.trigger_projectors import (
    normalize_repository,
    normalize_string_id,
)
from orchestra.provider_triggers.trigger_registry import CanonicalTriggerEvent


def _normalize_resource_value(*, normalization: str, value: Any) -> str | None:
    if normalization == "repository":
        return normalize_repository(value)
    if normalization in {"string", "item_id", "author", "title"}:
        if normalization == "item_id":
            return normalize_string_id(value)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    return None


def resolve_resource_id_from_filters(
    event: CanonicalTriggerEvent,
    filters: Sequence[Mapping[str, Any]] | None,
) -> str | None:
    """Derive the pinned resource id from authored filters using registry policy."""

    if not filters or not event.resource_filter_field:
        return None
    definition = event.filter_definition(event.resource_filter_field)
    if definition is None:
        return None
    for item in filters:
        field_name = str(item.get("field", "")).strip()
        if field_name != event.resource_filter_field:
            continue
        operator = str(item.get("operator", "")).strip()
        if operator == event.resource_filter_operator:
            return _normalize_resource_value(
                normalization=definition.normalization,
                value=item.get("value"),
            )
        if operator == "is any of" and event.resource_filter_operator == "is":
            value = item.get("value")
            if isinstance(value, list) and len(value) == 1:
                return _normalize_resource_value(
                    normalization=definition.normalization,
                    value=value[0],
                )
    return None
