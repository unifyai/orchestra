"""Clean up old rate limit counter records.

Runs periodically via scheduled job to remove rate limit records older
than the cleanup threshold (default: 48 hours). This keeps the
rate_limit_counter table small while maintaining the 24-hour rolling
window for rate limiting.

Scheduling Options:
1. GitHub Actions: .github/workflows/cleanup-rate-limit-records.yml
   - Runs daily at 3:00 AM UTC via cron: '0 3 * * *'
   - Calls POST /v0/admin/rate-limits/cleanup

2. Cloud Scheduler: Create a job to call the admin endpoint
   - Schedule: Daily or every 12 hours
   - Endpoint: POST /v0/admin/rate-limits/cleanup
   - Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>

3. Manual: Call the admin endpoint directly for one-off cleanup
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.rate_limit_counter_dao import RateLimitCounterDAO
from orchestra.web.api.utils.auth_rate_limiting import cleanup_auth_rate_limit_entries
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


def cleanup_rate_limit_records(session: Optional[Session] = None) -> int:
    """
    Remove rate limit counter records older than the cleanup threshold.

    Args:
        session: Optional database session. If not provided, creates one.

    Returns:
        Number of records deleted.
    """
    if session is not None:
        return _cleanup_in_session(session)
    else:
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        with SessionLocal() as session:
            return _cleanup_in_session(session)


def _cleanup_in_session(session: Session) -> int:
    """Internal function to clean up rate limit records within a session."""
    try:
        dao = RateLimitCounterDAO(session)
        deleted_count = dao.cleanup_old_buckets()

        auth_deleted = cleanup_auth_rate_limit_entries(session)
        deleted_count += auth_deleted

        session.commit()

        if deleted_count > 0:
            logger.info(
                f"Rate limit cleanup: removed {deleted_count} old records",
            )
        else:
            logger.debug("Rate limit cleanup: no old records to remove")

        return deleted_count

    except Exception as e:
        logger.error(f"Rate limit cleanup failed: {e}")
        session.rollback()
        raise


def get_rate_limit_stats(session: Optional[Session] = None) -> dict:
    """
    Get statistics about rate limit records for monitoring.

    Args:
        session: Optional database session. If not provided, creates one.

    Returns:
        Dictionary with stats like total_records, records_by_category, etc.
    """
    if session is not None:
        return _get_stats_in_session(session)
    else:
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        with SessionLocal() as session:
            return _get_stats_in_session(session)


def _get_stats_in_session(session: Session) -> dict:
    """Internal function to get stats within a session."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from orchestra.db.models.orchestra_models import RateLimitCounter

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_48h = now - timedelta(hours=48)

    # Total records
    total_records = session.query(func.count(RateLimitCounter.id)).scalar() or 0

    # Records in active window (last 24h)
    active_records = (
        session.query(func.count(RateLimitCounter.id))
        .filter(RateLimitCounter.time_bucket >= cutoff_24h)
        .scalar()
        or 0
    )

    # Records eligible for cleanup (older than 48h)
    cleanup_eligible = (
        session.query(func.count(RateLimitCounter.id))
        .filter(RateLimitCounter.time_bucket < cutoff_48h)
        .scalar()
        or 0
    )

    # Records by category
    category_counts = (
        session.query(
            RateLimitCounter.endpoint_category,
            func.count(RateLimitCounter.id),
        )
        .filter(RateLimitCounter.time_bucket >= cutoff_24h)
        .group_by(RateLimitCounter.endpoint_category)
        .all()
    )

    # Unique users in last 24h
    unique_users = (
        session.query(func.count(func.distinct(RateLimitCounter.user_id)))
        .filter(RateLimitCounter.time_bucket >= cutoff_24h)
        .scalar()
        or 0
    )

    return {
        "total_records": total_records,
        "active_records_24h": active_records,
        "cleanup_eligible_48h": cleanup_eligible,
        "unique_users_24h": unique_users,
        "records_by_category": {row[0]: row[1] for row in category_counts},
    }
