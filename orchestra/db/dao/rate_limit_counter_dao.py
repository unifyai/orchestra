"""DAO for rate limit counter operations.

This module provides data access operations for the rate_limit_counter table,
which tracks API request counts in 5-minute time buckets for rate limiting.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import RateLimitCounter


class RateLimitCounterDAO:
    """
    Data Access Object for rate limit counter operations.

    Handles recording requests, querying usage counts, calculating reset times,
    and cleaning up old data.
    """

    # Time bucket size in minutes
    BUCKET_SIZE_MINUTES = 5

    # Rolling window size in hours
    WINDOW_SIZE_HOURS = 24

    # Cleanup threshold in hours (delete buckets older than this)
    CLEANUP_THRESHOLD_HOURS = 48

    def __init__(self, session: Session):
        self.session = session

    def _get_time_bucket(self, dt: Optional[datetime] = None) -> datetime:
        """
        Truncate a datetime to the start of its 5-minute bucket.

        Args:
            dt: Datetime to truncate (defaults to now)

        Returns:
            Datetime truncated to 5-minute boundary
        """
        if dt is None:
            dt = datetime.now(timezone.utc)

        # Ensure timezone aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Truncate to 5-minute bucket
        minute = (dt.minute // self.BUCKET_SIZE_MINUTES) * self.BUCKET_SIZE_MINUTES
        return dt.replace(minute=minute, second=0, microsecond=0)

    def record_request(
        self,
        user_id: str,
        endpoint_category: str,
        endpoint_path: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> int:
        """
        Record a request in the rate limit counter using upsert.

        Increments the request count for the current 5-minute bucket,
        creating a new row if one doesn't exist.

        Args:
            user_id: ID of the user making the request
            endpoint_category: Rate limit category ('hiring', 'media', 'crud', 'voice')
            endpoint_path: Optional specific endpoint path for per-endpoint tracking
            organization_id: Optional organization context for shared limits

        Returns:
            The new request count for this bucket
        """
        time_bucket = self._get_time_bucket()

        # Use PostgreSQL INSERT ... ON CONFLICT for atomic upsert
        stmt = insert(RateLimitCounter).values(
            user_id=user_id,
            organization_id=organization_id,
            endpoint_category=endpoint_category,
            endpoint_path=endpoint_path,
            time_bucket=time_bucket,
            request_count=1,
        )

        # On conflict, increment the count
        stmt = stmt.on_conflict_do_update(
            constraint="uq_rate_limit_counter",
            set_={"request_count": RateLimitCounter.request_count + 1},
        )

        self.session.execute(stmt)

        # Return the new count (optional, mainly for testing)
        result = self.session.execute(
            select(RateLimitCounter.request_count).where(
                RateLimitCounter.user_id == user_id,
                RateLimitCounter.endpoint_category == endpoint_category,
                (
                    RateLimitCounter.endpoint_path == endpoint_path
                    if endpoint_path
                    else RateLimitCounter.endpoint_path.is_(None)
                ),
                RateLimitCounter.time_bucket == time_bucket,
            ),
        ).scalar()

        return result or 1

    def get_request_count_24h(
        self,
        user_id: str,
        endpoint_category: str,
        endpoint_path: Optional[str] = None,
        organization_id: Optional[int] = None,
        use_org_limit: bool = False,
    ) -> int:
        """
        Get the total request count in the last 24 hours.

        Args:
            user_id: ID of the user
            endpoint_category: Rate limit category to check
            endpoint_path: Optional specific endpoint path (for per-endpoint limits)
            organization_id: Optional organization ID
            use_org_limit: If True, count all org members' requests (shared limit)

        Returns:
            Total request count in the rolling 24-hour window
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.WINDOW_SIZE_HOURS)

        query = select(func.coalesce(func.sum(RateLimitCounter.request_count), 0))

        # Filter by entity (user or org for shared limits)
        if use_org_limit and organization_id is not None:
            # Organization shared limit: count all requests from org members
            query = query.where(RateLimitCounter.organization_id == organization_id)
        else:
            # User-level limit
            query = query.where(RateLimitCounter.user_id == user_id)

        # Filter by category
        query = query.where(RateLimitCounter.endpoint_category == endpoint_category)

        # Filter by time window
        query = query.where(RateLimitCounter.time_bucket >= cutoff)

        # Filter by endpoint path if specified
        if endpoint_path is not None:
            query = query.where(RateLimitCounter.endpoint_path == endpoint_path)
        else:
            # Category-level: count all paths in the category
            # (including both NULL and specific paths)
            pass

        result = self.session.execute(query).scalar()
        return int(result) if result else 0

    def get_reset_time(
        self,
        user_id: str,
        endpoint_category: str,
        organization_id: Optional[int] = None,
        use_org_limit: bool = False,
    ) -> datetime:
        """
        Calculate when the rate limit will reset.

        The reset time is when the oldest request in the current 24-hour window
        falls off (i.e., oldest_bucket + 24 hours).

        Args:
            user_id: ID of the user
            endpoint_category: Rate limit category
            organization_id: Optional organization ID
            use_org_limit: If True, check org-level oldest bucket

        Returns:
            Datetime when the rate limit will reset (allow one more request)
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.WINDOW_SIZE_HOURS)

        query = select(func.min(RateLimitCounter.time_bucket)).where(
            RateLimitCounter.time_bucket >= cutoff,
            RateLimitCounter.endpoint_category == endpoint_category,
            RateLimitCounter.request_count > 0,
        )

        if use_org_limit and organization_id is not None:
            query = query.where(RateLimitCounter.organization_id == organization_id)
        else:
            query = query.where(RateLimitCounter.user_id == user_id)

        oldest_bucket = self.session.execute(query).scalar()

        if oldest_bucket:
            # Ensure timezone aware
            if oldest_bucket.tzinfo is None:
                oldest_bucket = oldest_bucket.replace(tzinfo=timezone.utc)
            return oldest_bucket + timedelta(hours=self.WINDOW_SIZE_HOURS)

        # No requests in window - reset is now
        return datetime.now(timezone.utc)

    def get_seconds_until_reset(
        self,
        user_id: str,
        endpoint_category: str,
        organization_id: Optional[int] = None,
        use_org_limit: bool = False,
    ) -> int:
        """
        Get the number of seconds until the rate limit resets.

        Args:
            user_id: ID of the user
            endpoint_category: Rate limit category
            organization_id: Optional organization ID
            use_org_limit: If True, check org-level reset

        Returns:
            Seconds until reset (0 if already reset)
        """
        reset_time = self.get_reset_time(
            user_id=user_id,
            endpoint_category=endpoint_category,
            organization_id=organization_id,
            use_org_limit=use_org_limit,
        )

        seconds = (reset_time - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(seconds))

    def cleanup_old_buckets(self) -> int:
        """
        Delete rate limit counter buckets older than the cleanup threshold.

        This should be called periodically (e.g., hourly) to keep the table
        size bounded.

        Returns:
            Number of rows deleted
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.CLEANUP_THRESHOLD_HOURS,
        )

        result = self.session.execute(
            delete(RateLimitCounter).where(RateLimitCounter.time_bucket < cutoff),
        )

        return result.rowcount

    def get_usage_summary(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> dict:
        """
        Get a summary of rate limit usage for all categories.

        Useful for debugging and displaying to users.

        Args:
            user_id: ID of the user
            organization_id: Optional organization ID

        Returns:
            Dict mapping category to usage count
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.WINDOW_SIZE_HOURS)

        query = (
            select(
                RateLimitCounter.endpoint_category,
                func.sum(RateLimitCounter.request_count),
            )
            .where(
                RateLimitCounter.user_id == user_id,
                RateLimitCounter.time_bucket >= cutoff,
            )
            .group_by(RateLimitCounter.endpoint_category)
        )

        results = self.session.execute(query).all()

        return {row[0]: int(row[1]) for row in results}
