"""Retention policy for matched provider-event context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestra.settings import settings


def resolve_event_context_expires_at(
    *,
    now: datetime | None = None,
) -> datetime:
    """Return the UTC instant when one accepted event context should expire."""

    anchor = now or datetime.now(timezone.utc)
    return anchor + timedelta(days=settings.trigger_event_context_retention_days)
