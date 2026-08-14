"""Address-based rate limiting for unauthenticated auth endpoints.

Throttles auth endpoints by the caller's address, optionally combined
with an identifier (email, user_id). Uses the same 5-minute time bucket
pattern as the existing RateLimitCounter system but doesn't require an
authenticated user.

**The address arrives explicitly, never from the request.** Every auth
call reaches Orchestra through Console's Next.js server on an admin-key
endpoint, so the transport request describes Console: one egress address
shared by every signup on the platform. Keying on it does not throttle a
caller, it collapses the platform onto a single counter — a limit of
thirty signups a day between everyone, which then holds itself closed
because each rejected retry extends the window it is measured against.

Console holds the browser's request and passes what it saw. Unlike the
advisory record in :mod:`signup_provenance`, this value is a security
control, so Console derives it from the hop its load balancer appended
rather than the left-most hop a caller supplies: whatever reaches this
module must be an address the caller could not choose for themselves.

Usage in endpoint handlers:

    def my_endpoint(body: MyRequest, session: Session = ...):
        enforce_auth_rate_limit(
            session, body.client_ip, "auth_login",
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
    """The address a request arrived from.

    For callers that reach Orchestra directly, such as inbound provider
    webhooks. Auth endpoints must not use this: they are reached through
    Console, so it describes Console rather than the person signing up.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def subnet_of(ip: str) -> str:
    """Collapse an address to its /24 (IPv4) or /48 (IPv6) prefix.

    Farmers rotating addresses within one allocation share a prefix even
    when individual addresses differ.
    """
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::/48"
    parts = ip.rsplit(".", 1)
    return f"{parts[0]}.0/24" if len(parts) == 2 else ip


def enforce_auth_rate_limit(
    session: Session,
    client_ip: Optional[str],
    category: str,
    max_attempts: int,
    window_minutes: int = 5,
    identifier: Optional[str] = None,
    use_subnet: bool = False,
) -> None:
    """
    Raise 429 if this caller is over the limit, otherwise record the attempt.

    Call this at the top of auth endpoint handlers, after the body is parsed.

    Args:
        session: DB session.
        client_ip: The caller's address as Console observed it, or None
            when Console could not determine one.
        category: Rate limit category (e.g. 'auth_login').
        max_attempts: Max allowed attempts within the window.
        window_minutes: Rolling window size in minutes.
        identifier: Optional secondary key (email, user_id) combined with
            the address.
        use_subnet: Key on the caller's /24 (or IPv6 /48) prefix instead
            of the exact address, so limits hold across a rotating
            allocation.
    """
    if settings.is_staging or settings.environment == "dev":
        return

    if client_ip:
        ip = subnet_of(client_ip) if use_subnet else client_ip
        key = f"{ip}:{identifier}" if identifier else ip
    elif identifier:
        key = identifier
    else:
        # Console sends an address on every proxied auth route, so an
        # absence is a bug there rather than something a caller can
        # arrange. A limit with nothing else to key on would put every
        # signup on the platform behind one counter, which is the
        # failure this argument exists to end.
        logger.warning("No client address for %s — check the Console route", category)
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    total = session.execute(
        select(func.coalesce(func.sum(AuthRateLimitEntry.attempt_count), 0)).where(
            AuthRateLimitEntry.key == key,
            AuthRateLimitEntry.endpoint_category == category,
            AuthRateLimitEntry.time_bucket >= cutoff,
        ),
    ).scalar()

    # Counted before the attempt is recorded. Recording first lets a
    # rejected attempt extend the window it is then measured against, so
    # a caller retrying against a closed limit holds it closed for as
    # long as they keep trying.
    if total and int(total) >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": "Too many requests. Please try again later.",
                "retry_after_seconds": 60,
            },
        )

    stmt = insert(AuthRateLimitEntry).values(
        key=key,
        endpoint_category=category,
        time_bucket=_get_time_bucket(),
        attempt_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_auth_rate_limit_entry",
        set_={"attempt_count": AuthRateLimitEntry.attempt_count + 1},
    )
    session.execute(stmt)
    session.flush()


def cleanup_auth_rate_limit_entries(session: Session) -> int:
    """Delete auth rate limit entries older than 48 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    result = session.execute(
        delete(AuthRateLimitEntry).where(AuthRateLimitEntry.time_bucket < cutoff),
    )
    return result.rowcount
