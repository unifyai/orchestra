"""IP-based rate limiting for unauthenticated auth endpoints.

Provides helpers that throttle auth endpoints by client IP, optionally
combined with an identifier (email, user_id). Uses the same 5-minute
time bucket pattern as the existing RateLimitCounter system but doesn't
require an authenticated user.

Usage in endpoint handlers:

    def my_endpoint(body: MyRequest, request: Request, session: Session = ...):
        enforce_auth_rate_limit(
            session, request, "auth_login",
            max_attempts=10, identifier=body.email,
        )
        ...
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import AuthRateLimitEntry
from orchestra.settings import settings

logger = logging.getLogger(__name__)

BUCKET_SIZE_MINUTES = 5


def _get_time_bucket(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minute = (dt.minute // BUCKET_SIZE_MINUTES) * BUCKET_SIZE_MINUTES
    return dt.replace(minute=minute, second=0, microsecond=0)


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_auth_rate_limit(
    session: Session,
    request: Request,
    category: str,
    max_attempts: int,
    window_minutes: int = 5,
    identifier: Optional[str] = None,
) -> None:
    """
    Record an auth attempt and raise 429 if the limit is exceeded.

    Call this at the top of auth endpoint handlers, after the body is parsed.

    Args:
        session: DB session.
        request: FastAPI request (used to extract client IP).
        category: Rate limit category (e.g. 'auth_login').
        max_attempts: Max allowed attempts within the window.
        window_minutes: Rolling window size in minutes.
        identifier: Optional secondary key (email, user_id) combined with IP.
    """
    if settings.is_staging or settings.environment == "dev":
        return

    ip = get_client_ip(request)
    key = f"{ip}:{identifier}" if identifier else ip
    bucket = _get_time_bucket()

    stmt = insert(AuthRateLimitEntry).values(
        key=key,
        endpoint_category=category,
        time_bucket=bucket,
        attempt_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_auth_rate_limit_entry",
        set_={"attempt_count": AuthRateLimitEntry.attempt_count + 1},
    )
    session.execute(stmt)
    session.flush()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    total = session.execute(
        select(func.coalesce(func.sum(AuthRateLimitEntry.attempt_count), 0)).where(
            AuthRateLimitEntry.key == key,
            AuthRateLimitEntry.endpoint_category == category,
            AuthRateLimitEntry.time_bucket >= cutoff,
        ),
    ).scalar()

    if total and int(total) > max_attempts:
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": "Too many requests. Please try again later.",
                "retry_after_seconds": 60,
            },
        )


def cleanup_auth_rate_limit_entries(session: Session) -> int:
    """Delete auth rate limit entries older than 48 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    result = session.execute(
        delete(AuthRateLimitEntry).where(AuthRateLimitEntry.time_bucket < cutoff),
    )
    return result.rowcount
