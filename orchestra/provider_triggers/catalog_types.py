"""Vocabulary for provider trigger catalog import."""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class TriggerCatalogImportStatus(StrEnum):
    """Last import outcome for one backend/environment bootstrap row."""

    pending = "pending"
    imported = "imported"
    skipped = "skipped"
    failed = "failed"
