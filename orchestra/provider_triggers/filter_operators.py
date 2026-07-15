"""Closed vocabularies for curated provider-event filter operators."""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class FilterOperator(StrEnum):
    """Deterministic operators exposed by the curated trigger registry."""

    is_ = "is"
    is_not = "is not"
    is_any_of = "is any of"
    includes = "includes"
    excludes = "excludes"
    contains = "contains"
    does_not_contain = "does not contain"
