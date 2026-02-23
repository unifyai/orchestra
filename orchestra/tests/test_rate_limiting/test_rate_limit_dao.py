"""
Tests for RateLimitCounterDAO.

These tests stress-test the rate limit counter DAO including:
- Recording requests with upsert logic
- Counting requests in rolling 24h window
- Time bucket precision (5-minute buckets)
- Reset time calculation
- Organization shared limits
- Cleanup of old buckets
- Edge cases and concurrent access
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.rate_limit_counter_dao import RateLimitCounterDAO
from orchestra.db.models.orchestra_models import Organization, RateLimitCounter, User

# ===========================================================================
# Test Fixtures
# ===========================================================================


@pytest.fixture
def rate_limit_dao(dbsession: Session) -> RateLimitCounterDAO:
    """Create a RateLimitCounterDAO instance for testing."""
    return RateLimitCounterDAO(dbsession)


@pytest.fixture
def session(dbsession: Session) -> Session:
    """Alias for dbsession for cleaner test signatures."""
    return dbsession


@pytest.fixture
def test_user_id(dbsession: Session) -> str:
    """Create a test user and return their ID."""
    user_id = "test-user-rate-limit-001"
    # Create user if not exists
    existing = dbsession.query(User).filter(User.id == user_id).first()
    if not existing:
        user = User(id=user_id, email="rate-limit-test@example.com")
        dbsession.add(user)
        dbsession.commit()
    return user_id


@pytest.fixture
def test_org_id(dbsession: Session, test_user_id: str) -> int:
    """Create a test organization and return its ID."""
    # Check if org exists with a high ID to avoid conflicts
    org = dbsession.query(Organization).filter(Organization.id == 99999).first()
    if not org:
        org = Organization(id=99999, name="Test Rate Limit Org", owner_id=test_user_id)
        dbsession.add(org)
        dbsession.commit()
    return org.id


# ===========================================================================
# Time Bucket Tests
# ===========================================================================


class TestTimeBucket:
    """Tests for time bucket calculation."""

    def test_truncates_to_five_minute_boundary(
        self,
        rate_limit_dao: RateLimitCounterDAO,
    ):
        """Time should be truncated to 5-minute boundaries."""
        # 14:17:45 should become 14:15:00
        dt = datetime(2026, 2, 12, 14, 17, 45, 123456, tzinfo=timezone.utc)
        bucket = rate_limit_dao._get_time_bucket(dt)

        assert bucket.minute == 15
        assert bucket.second == 0
        assert bucket.microsecond == 0

    def test_exact_five_minute_boundary(self, rate_limit_dao: RateLimitCounterDAO):
        """Exact 5-minute boundary should stay the same."""
        dt = datetime(2026, 2, 12, 14, 15, 0, 0, tzinfo=timezone.utc)
        bucket = rate_limit_dao._get_time_bucket(dt)

        assert bucket.minute == 15
        assert bucket == dt

    def test_zero_minute_boundary(self, rate_limit_dao: RateLimitCounterDAO):
        """Minutes 0-4 should truncate to 0."""
        dt = datetime(2026, 2, 12, 14, 3, 30, tzinfo=timezone.utc)
        bucket = rate_limit_dao._get_time_bucket(dt)

        assert bucket.minute == 0

    def test_end_of_hour(self, rate_limit_dao: RateLimitCounterDAO):
        """Minutes 55-59 should truncate to 55."""
        dt = datetime(2026, 2, 12, 14, 58, 30, tzinfo=timezone.utc)
        bucket = rate_limit_dao._get_time_bucket(dt)

        assert bucket.minute == 55

    def test_naive_datetime_gets_utc(self, rate_limit_dao: RateLimitCounterDAO):
        """Naive datetime should be treated as UTC."""
        dt = datetime(2026, 2, 12, 14, 17, 45)
        bucket = rate_limit_dao._get_time_bucket(dt)

        assert bucket.tzinfo == timezone.utc

    def test_default_to_now(self, rate_limit_dao: RateLimitCounterDAO):
        """Calling without argument should use current time."""
        bucket = rate_limit_dao._get_time_bucket()
        now = datetime.now(timezone.utc)

        # Should be within 5 minutes of now
        assert abs((bucket - now).total_seconds()) < 300


# ===========================================================================
# Request Recording Tests
# ===========================================================================


class TestRecordRequest:
    """Tests for recording requests."""

    def test_first_request_creates_record(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """First request should create a new record with count 1."""
        count = rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        assert count == 1

        # Verify in database
        record = (
            session.query(RateLimitCounter)
            .filter(
                RateLimitCounter.user_id == test_user_id,
                RateLimitCounter.endpoint_category == "assistant_media",
            )
            .first()
        )

        assert record is not None
        assert record.request_count == 1

    def test_subsequent_requests_increment_count(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Multiple requests in same bucket should increment count."""
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        # Verify total count in 24h window
        count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        assert count == 3

    def test_different_categories_separate_counts(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
    ):
        """Different categories should have separate counts."""
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_hiring",
        )

        media_count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        hiring_count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_hiring",
        )

        assert media_count == 2
        assert hiring_count == 1

    def test_endpoint_path_tracked_separately(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
    ):
        """Different endpoint paths should be tracked separately."""
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            endpoint_path="/v0/assistant/photo/generate",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            endpoint_path="/v0/assistant/photo/generate",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            endpoint_path="/v0/assistant/photo/edit",
        )

        # Category-level count should include all
        total = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        assert total == 3

        # Per-endpoint count
        generate_count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            endpoint_path="/v0/assistant/photo/generate",
        )
        assert generate_count == 2

    def test_organization_id_tracked(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        test_org_id: int,
        session: Session,
    ):
        """Organization ID should be stored with the request."""
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            organization_id=test_org_id,
        )

        record = (
            session.query(RateLimitCounter)
            .filter(
                RateLimitCounter.user_id == test_user_id,
            )
            .first()
        )

        assert record.organization_id == test_org_id


# ===========================================================================
# Request Counting Tests (24h Window)
# ===========================================================================


class TestRequestCount24h:
    """Tests for counting requests in rolling 24h window."""

    def test_counts_requests_in_window(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Should count all requests in the last 24 hours."""
        # Record some requests
        for _ in range(5):
            rate_limit_dao.record_request(
                user_id=test_user_id,
                endpoint_category="assistant_media",
            )

        count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        assert count == 5

    def test_excludes_old_requests(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Requests older than 24 hours should not be counted."""
        # Manually insert an old record
        old_bucket = datetime.now(timezone.utc) - timedelta(hours=25)
        old_record = RateLimitCounter(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            time_bucket=old_bucket,
            request_count=10,
        )
        session.add(old_record)
        session.commit()

        # Record a new request
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        count = rate_limit_dao.get_request_count_24h(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        # Should only count the new request, not the old one
        assert count == 1

    def test_org_shared_limit(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_org_id: int,
        session: Session,
    ):
        """Organization shared limit should count all org members' requests."""
        # Two different users in same org - create them first
        user1 = "org-user-1"
        user2 = "org-user-2"

        # Create test users
        for uid, email in [
            (user1, "org-user-1@test.com"),
            (user2, "org-user-2@test.com"),
        ]:
            if not session.query(User).filter(User.id == uid).first():
                session.add(User(id=uid, email=email))
        session.commit()

        rate_limit_dao.record_request(
            user_id=user1,
            endpoint_category="assistant_hiring",
            organization_id=test_org_id,
        )
        rate_limit_dao.record_request(
            user_id=user1,
            endpoint_category="assistant_hiring",
            organization_id=test_org_id,
        )
        rate_limit_dao.record_request(
            user_id=user2,
            endpoint_category="assistant_hiring",
            organization_id=test_org_id,
        )

        # User-level count (only user1's requests)
        user1_count = rate_limit_dao.get_request_count_24h(
            user_id=user1,
            endpoint_category="assistant_hiring",
            organization_id=test_org_id,
            use_org_limit=False,
        )
        assert user1_count == 2

        # Org-level count (all org members)
        org_count = rate_limit_dao.get_request_count_24h(
            user_id=user1,
            endpoint_category="assistant_hiring",
            organization_id=test_org_id,
            use_org_limit=True,
        )
        assert org_count == 3

    def test_zero_count_for_no_requests(
        self,
        rate_limit_dao: RateLimitCounterDAO,
    ):
        """Should return 0 for users with no requests."""
        count = rate_limit_dao.get_request_count_24h(
            user_id="nonexistent-user",
            endpoint_category="assistant_media",
        )

        assert count == 0


# ===========================================================================
# Reset Time Tests
# ===========================================================================


class TestResetTime:
    """Tests for reset time calculation."""

    def test_reset_time_is_oldest_bucket_plus_24h(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Reset time should be when oldest bucket falls off window."""
        # Insert a bucket from 20 hours ago
        old_bucket = datetime.now(timezone.utc) - timedelta(hours=20)
        old_bucket = rate_limit_dao._get_time_bucket(old_bucket)

        record = RateLimitCounter(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            time_bucket=old_bucket,
            request_count=5,
        )
        session.add(record)
        session.commit()

        reset_time = rate_limit_dao.get_reset_time(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        # Reset should be ~4 hours from now (24 - 20)
        expected_reset = old_bucket + timedelta(hours=24)
        assert abs((reset_time - expected_reset).total_seconds()) < 60

    def test_reset_time_now_when_no_requests(
        self,
        rate_limit_dao: RateLimitCounterDAO,
    ):
        """Reset time should be now when no requests in window."""
        reset_time = rate_limit_dao.get_reset_time(
            user_id="nonexistent-user",
            endpoint_category="assistant_media",
        )

        now = datetime.now(timezone.utc)
        assert abs((reset_time - now).total_seconds()) < 5

    def test_seconds_until_reset(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Should correctly calculate seconds until reset."""
        # Insert a bucket from 23 hours ago (1 hour until reset)
        old_bucket = datetime.now(timezone.utc) - timedelta(hours=23)
        old_bucket = rate_limit_dao._get_time_bucket(old_bucket)

        record = RateLimitCounter(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            time_bucket=old_bucket,
            request_count=5,
        )
        session.add(record)
        session.commit()

        seconds = rate_limit_dao.get_seconds_until_reset(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        # Should be approximately 1 hour (3600 seconds) +/- 5 minutes
        # Use <= for boundary condition
        assert 3300 <= seconds <= 3900


# ===========================================================================
# Cleanup Tests
# ===========================================================================


class TestCleanup:
    """Tests for cleaning up old buckets."""

    def test_cleanup_deletes_old_buckets(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
        session: Session,
    ):
        """Cleanup should delete buckets older than threshold."""
        # Insert old buckets (50 hours ago - beyond 48h threshold)
        old_bucket = datetime.now(timezone.utc) - timedelta(hours=50)
        old_record = RateLimitCounter(
            user_id=test_user_id,
            endpoint_category="assistant_media",
            time_bucket=old_bucket,
            request_count=10,
        )
        session.add(old_record)

        # Insert recent bucket
        recent_bucket = datetime.now(timezone.utc) - timedelta(hours=10)
        recent_record = RateLimitCounter(
            user_id=test_user_id,
            endpoint_category="assistant_crud",
            time_bucket=recent_bucket,
            request_count=5,
        )
        session.add(recent_record)
        session.commit()

        # Run cleanup
        deleted = rate_limit_dao.cleanup_old_buckets()

        assert deleted == 1

        # Verify old is gone, recent remains
        remaining = (
            session.query(RateLimitCounter)
            .filter(
                RateLimitCounter.user_id == test_user_id,
            )
            .all()
        )

        assert len(remaining) == 1
        assert remaining[0].endpoint_category == "assistant_crud"

    def test_cleanup_returns_zero_when_nothing_to_delete(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
    ):
        """Cleanup should return 0 when no old buckets exist."""
        # Record a recent request
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )

        deleted = rate_limit_dao.cleanup_old_buckets()

        assert deleted == 0


# ===========================================================================
# Usage Summary Tests
# ===========================================================================


class TestUsageSummary:
    """Tests for usage summary."""

    def test_returns_all_categories(
        self,
        rate_limit_dao: RateLimitCounterDAO,
        test_user_id: str,
    ):
        """Should return counts for all used categories."""
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_media",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_hiring",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_crud",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_crud",
        )
        rate_limit_dao.record_request(
            user_id=test_user_id,
            endpoint_category="assistant_crud",
        )

        summary = rate_limit_dao.get_usage_summary(user_id=test_user_id)

        assert summary == {
            "assistant_media": 2,
            "assistant_hiring": 1,
            "assistant_crud": 3,
        }

    def test_empty_summary_for_new_user(
        self,
        rate_limit_dao: RateLimitCounterDAO,
    ):
        """Should return empty dict for users with no requests."""
        summary = rate_limit_dao.get_usage_summary(user_id="nonexistent-user")

        assert summary == {}
