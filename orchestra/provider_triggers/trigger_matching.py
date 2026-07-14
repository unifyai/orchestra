"""Deterministic filter matching over curated provider-event projections."""

from __future__ import annotations

import unicodedata
from typing import Any, Mapping, Sequence

from orchestra.provider_triggers.filter_operators import FilterField, FilterOperator
from orchestra.provider_triggers.trigger_registry import (
    CanonicalTriggerEvent,
    require_canonical_trigger_event,
)


def normalize_repository(value: Any) -> str | None:
    """Normalize a repository to canonical owner/name, case-folded."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        full_name = value.get("full_name") or value.get("fullName")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip().casefold()
        owner = value.get("owner")
        name = value.get("name") or value.get("repo")
        owner_login = (
            owner.get("login")
            if isinstance(owner, Mapping)
            else owner if isinstance(owner, str) else None
        )
        if isinstance(owner_login, str) and isinstance(name, str):
            joined = f"{owner_login.strip()}/{name.strip()}"
            return joined.casefold() if joined.strip("/") else None
        return None
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    return None


def normalize_author(value: Any) -> str | None:
    """Normalize an author login, case-folded."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        login = value.get("login") or value.get("username") or value.get("name")
        if isinstance(login, str) and login.strip():
            return login.strip().casefold()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    return None


def normalize_labels(value: Any) -> list[str] | None:
    """Normalize label names to a case-folded list, or None when absent."""

    if value is None:
        return None
    if isinstance(value, str):
        return [value.strip().casefold()] if value.strip() else None
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return None
    labels: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                labels.append(name.strip().casefold())
        elif isinstance(item, str) and item.strip():
            labels.append(item.strip().casefold())
    return labels


def normalize_title(value: Any) -> str | None:
    """Normalize title text with NFKC and case-folding."""

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return unicodedata.normalize("NFKC", value).casefold()


def normalize_filter_value(field: FilterField, value: Any) -> Any:
    """Normalize one authored filter value for comparison."""

    if field is FilterField.labels:
        if isinstance(value, str):
            return normalize_labels([value])
        return normalize_labels(value)
    if field is FilterField.repository:
        return normalize_repository(value)
    if field is FilterField.author:
        return normalize_author(value)
    if field is FilterField.title:
        return normalize_title(value)
    return value


def project_github_issue_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the curated projection for github.issue_created from source data.

    Unknown source fields are omitted from the projection and therefore cannot
    affect matching; callers retain the full source body separately.

    TODO: Replace this single-event helper with a registry-keyed projector map
    so each curated event_slug owns its projection without adapter branching.
    """

    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        data = {}
    issue = data.get("issue") if isinstance(data.get("issue"), Mapping) else {}
    repository = data.get("repository")
    if repository is None and isinstance(issue, Mapping):
        repository = issue.get("repository")
    author = data.get("user")
    if author is None and isinstance(issue, Mapping):
        author = issue.get("user")
    title = data.get("title")
    if title is None and isinstance(issue, Mapping):
        title = issue.get("title")
    labels = data.get("labels")
    if labels is None and isinstance(issue, Mapping):
        labels = issue.get("labels")
    return {
        "repository": normalize_repository(repository),
        "author": normalize_author(author),
        "labels": normalize_labels(labels),
        "title": normalize_title(title),
    }


def evaluate_filter(
    *,
    field: FilterField,
    operator: FilterOperator,
    expected: Any,
    actual: Any,
) -> bool:
    """Evaluate one curated filter. Missing actual values fail every operator."""

    if actual is None:
        return False

    if operator is FilterOperator.is_:
        return actual == normalize_filter_value(field, expected)
    if operator is FilterOperator.is_not:
        return actual != normalize_filter_value(field, expected)
    if operator is FilterOperator.is_any_of:
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            return False
        normalized_expected = [normalize_filter_value(field, item) for item in expected]
        return actual in normalized_expected
    if operator is FilterOperator.includes:
        needle = normalize_filter_value(FilterField.labels, expected)
        if not isinstance(actual, list) or not needle:
            return False
        if isinstance(needle, list):
            return all(item in actual for item in needle)
        return False
    if operator is FilterOperator.excludes:
        needle = normalize_filter_value(FilterField.labels, expected)
        if not isinstance(actual, list) or not needle:
            return False
        if isinstance(needle, list):
            return all(item not in actual for item in needle)
        return False
    if operator is FilterOperator.contains:
        haystack = actual if isinstance(actual, str) else None
        needle = normalize_filter_value(FilterField.title, expected)
        if haystack is None or needle is None:
            return False
        return needle in haystack
    if operator is FilterOperator.does_not_contain:
        haystack = actual if isinstance(actual, str) else None
        needle = normalize_filter_value(FilterField.title, expected)
        if haystack is None or needle is None:
            return False
        return needle not in haystack
    return False


def matches_filters(
    *,
    projection: Mapping[str, Any],
    filters: Sequence[Mapping[str, Any]] | None,
    event: CanonicalTriggerEvent | None = None,
    event_slug: str = "github.issue_created",
    schema_version: str = "1",
) -> bool:
    """Return True when every authored AND filter matches the projection."""

    if not filters:
        return True
    resolved = event or require_canonical_trigger_event(
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
        field = definition.field
        actual = projection.get(field.value)
        if not evaluate_filter(
            field=field,
            operator=operator,
            expected=item.get("value"),
            actual=actual,
        ):
            return False
    return True
