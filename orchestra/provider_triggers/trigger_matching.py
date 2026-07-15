"""Deterministic filter matching over curated provider-event projections."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.filter_operators import FilterOperator
from orchestra.provider_triggers.trigger_projectors import (
    normalize_author,
    normalize_labels,
    normalize_repository,
    normalize_string_id,
    normalize_title,
)
from orchestra.provider_triggers.trigger_registry import (
    CanonicalTriggerEvent,
    FilterFieldDefinition,
    require_canonical_trigger_event,
)


def normalize_filter_value(*, definition: FilterFieldDefinition, value: Any) -> Any:
    """Normalize one authored filter value for comparison."""

    normalization = definition.normalization
    if normalization == "labels":
        if isinstance(value, str):
            return normalize_labels([value])
        return normalize_labels(value)
    if normalization == "repository":
        return normalize_repository(value)
    if normalization == "author":
        return normalize_author(value)
    if normalization == "title":
        return normalize_title(value)
    if normalization == "item_id":
        return normalize_string_id(value)
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def evaluate_filter(
    *,
    definition: FilterFieldDefinition,
    operator: FilterOperator,
    expected: Any,
    actual: Any,
) -> bool:
    """Evaluate one curated filter. Missing actual values fail every operator."""

    if actual is None:
        return False

    if operator is FilterOperator.is_:
        return actual == normalize_filter_value(definition=definition, value=expected)
    if operator is FilterOperator.is_not:
        return actual != normalize_filter_value(definition=definition, value=expected)
    if operator is FilterOperator.is_any_of:
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            return False
        normalized_expected = [
            normalize_filter_value(definition=definition, value=item)
            for item in expected
        ]
        return actual in normalized_expected
    if operator is FilterOperator.includes:
        needle = normalize_filter_value(definition=definition, value=expected)
        if definition.value_type != "string_array" or not isinstance(actual, list):
            return False
        if not needle:
            return False
        if isinstance(needle, list):
            return all(item in actual for item in needle)
        return False
    if operator is FilterOperator.excludes:
        needle = normalize_filter_value(definition=definition, value=expected)
        if definition.value_type != "string_array" or not isinstance(actual, list):
            return False
        if not needle:
            return False
        if isinstance(needle, list):
            return all(item not in actual for item in needle)
        return False
    if operator is FilterOperator.contains:
        haystack = actual if isinstance(actual, str) else None
        needle = normalize_filter_value(definition=definition, value=expected)
        if haystack is None or needle is None:
            return False
        return needle in haystack
    if operator is FilterOperator.does_not_contain:
        haystack = actual if isinstance(actual, str) else None
        needle = normalize_filter_value(definition=definition, value=expected)
        if haystack is None or needle is None:
            return False
        return needle not in haystack
    return False


def matches_filters(
    *,
    projection: Mapping[str, Any],
    filters: Sequence[Mapping[str, Any]] | None,
    event: CanonicalTriggerEvent | None = None,
    event_slug: str | None = None,
    schema_version: str = "1",
) -> bool:
    """Return True when every authored AND filter matches the projection."""

    if not filters:
        return True
    resolved = event
    if resolved is None:
        if event_slug is None:
            raise ValueError("matches_filters requires event or event_slug")
        resolved = require_canonical_trigger_event(
            event_slug,
            schema_version=schema_version,
        )
    for item in filters:
        field_name = str(item.get("field", "")).strip()
        operator_name = str(item.get("operator", "")).strip()
        definition = resolved.filter_definition(field_name)
        if definition is None:
            return False
        try:
            operator = FilterOperator(operator_name)
        except ValueError:
            return False
        if operator not in definition.operators:
            return False
        actual = projection.get(definition.field)
        if not evaluate_filter(
            definition=definition,
            operator=operator,
            expected=item.get("value"),
            actual=actual,
        ):
            return False
    return True
