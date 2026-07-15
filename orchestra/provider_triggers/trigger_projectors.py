"""Registry-keyed curated projection builders for provider-event matching."""

from __future__ import annotations

import unicodedata
from typing import Any, Callable, Mapping

from orchestra.provider_triggers.trigger_registry import (
    GITHUB_ISSUE_CREATED,
    SYNTHETIC_ITEM_CREATED,
)

ProjectorFn = Callable[[Mapping[str, Any]], dict[str, Any]]


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
    if not isinstance(value, (list, tuple)) or isinstance(value, (bytes, bytearray)):
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

    if value is None or not isinstance(value, str) or not value.strip():
        return None
    return unicodedata.normalize("NFKC", value).casefold()


def normalize_string_id(value: Any) -> str | None:
    """Normalize a plain string identifier, case-folded."""

    if value is None or not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def project_github_issue_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the curated projection for github.issue_created from source data."""

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


def project_synthetic_item_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the curated projection for synthetic.item_created conformance tests."""

    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        data = payload
    return {
        "item_id": normalize_string_id(data.get("item_id")),
        "title": normalize_title(data.get("title")),
    }


PROJECTOR_REGISTRY: dict[str, ProjectorFn] = {
    GITHUB_ISSUE_CREATED: project_github_issue_created,
    SYNTHETIC_ITEM_CREATED: project_synthetic_item_created,
}


def project_curated_payload(
    *,
    projector_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the curated projection for one registry projector key."""

    projector = PROJECTOR_REGISTRY.get(projector_key)
    if projector is None:
        raise LookupError(f"Unsupported projector key {projector_key!r}")
    return projector(payload)
