"""Closed vocabularies for curated provider-event filter fields and operators."""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class FilterField(StrEnum):
    """Curated projection fields that authored filters may target.

    TODO: These members are the github.issue_created initial set. Prefer
    event-scoped field definitions from the registry over growing one global
    enum forever when new events need disjoint vocabularies.
    """

    repository = "repository"
    author = "author"
    labels = "labels"
    title = "title"


class FilterOperator(StrEnum):
    """Deterministic operators exposed by the curated trigger registry."""

    is_ = "is"
    is_not = "is not"
    is_any_of = "is any of"
    includes = "includes"
    excludes = "excludes"
    contains = "contains"
    does_not_contain = "does not contain"
