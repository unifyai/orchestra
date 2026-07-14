"""Stable client-facing reasons for provider-event context access."""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


class EventContextErrorReason(StrEnum):
    """Closed vocabulary for event-context HTTP and service failures."""

    unavailable = "event_context_unavailable"
    expired = "event_context_expired"
    deleted = "event_context_deleted"
    token_expired = "event_context_token_expired"
    invalid_audience = "invalid_event_context_audience"


class EventContextAccessError(Exception):
    """Raised when one event-context read/export/delete must fail closed."""

    def __init__(self, reason: EventContextErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)
