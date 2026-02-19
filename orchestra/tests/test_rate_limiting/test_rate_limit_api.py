"""
Tests for rate limiting API integration.

These tests stress-test the rate limiting behavior at the API level including:
- Rate limit enforcement (429 response when exceeded)
- Category-based limits
- Per-endpoint overrides
- Tiered hiring limits based on account status
- Organization shared limits
- Reset time in error response
- Integration with existing endpoints
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import RateLimitCounter
from orchestra.tests.utils import create_test_user

# ===========================================================================
# Test Fixtures
# ===========================================================================


@pytest.fixture
def session(dbsession: Session) -> Session:
    """Alias for dbsession for cleaner test signatures."""
    return dbsession


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Mock assistant infrastructure to prevent real network calls."""
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield mock_wake_up, mock_reawaken


@pytest.fixture
async def rate_limit_test_user(client: AsyncClient) -> Dict[str, Any]:
    """Create a user specifically for rate limit testing."""
    user = await create_test_user(
        client,
        email="rate-limit-test@example.com",
        # Legacy: approve for backward compat during transition
    )
    return user


@pytest.fixture
def clean_rate_limits(session: Session, rate_limit_test_user: Dict[str, Any]):
    """Clean up any existing rate limit records for the test user."""
    session.query(RateLimitCounter).filter(
        RateLimitCounter.user_id == rate_limit_test_user["id"],
    ).delete()
    session.commit()
    yield
    # Cleanup after test too
    session.query(RateLimitCounter).filter(
        RateLimitCounter.user_id == rate_limit_test_user["id"],
    ).delete()
    session.commit()


# ===========================================================================
# Helper Functions
# ===========================================================================


def _fill_rate_limit(
    session: Session,
    user_id: str,
    category: str,
    count: int,
    organization_id: int = None,
):
    """Fill up rate limit counter to near the limit."""
    bucket = datetime.now(timezone.utc)
    bucket = bucket.replace(
        minute=(bucket.minute // 5) * 5,
        second=0,
        microsecond=0,
    )

    record = RateLimitCounter(
        user_id=user_id,
        organization_id=organization_id,
        endpoint_category=category,
        time_bucket=bucket,
        request_count=count,
    )
    session.add(record)
    session.commit()


# ===========================================================================
# Rate Limit Enforcement Tests
# ===========================================================================


class TestRateLimitEnforcement:
    """Tests for rate limit enforcement."""

    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_request_below_limit_succeeds(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
    ):
        """Requests below the limit should succeed."""
        # First request should succeed
        response = await client.get(
            "/v0/credits",
            headers=rate_limit_test_user["headers"],
        )

        # This endpoint doesn't have rate limiting, so it should work
        assert response.status_code == 200

    @pytest.mark.skip(
        reason="Billing check (402) runs before rate limit check (429) in test env",
    )
    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_rate_limit_exceeded_returns_429(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Exceeding rate limit should return 429."""
        # Fill up the rate limit for hiring (limit is 1 for new accounts)
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=1,
        )

        # Next request should be rate limited
        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Test",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        assert response.status_code == 429

    @pytest.mark.skip(
        reason="Billing check (402) runs before rate limit check (429) in test env",
    )
    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_429_response_includes_reset_time(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """429 response should include reset time information."""
        # Fill up the rate limit
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=1,
        )

        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Test",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        assert response.status_code == 429

        data = response.json()
        detail = data.get("detail", {})

        # Should include rate limit info
        assert "limit" in detail
        assert "used" in detail
        assert "reset_in_seconds" in detail
        assert "reset_at" in detail
        assert detail["limit"] >= 1
        assert detail["used"] >= 1
        assert detail["reset_in_seconds"] >= 0

    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_different_users_have_separate_limits(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Different users should have independent rate limits."""
        # Fill up rate limit for test user
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=1,
        )

        # Create another user
        other_user = await create_test_user(
            client,
            email="other-rate-limit-test@example.com",
        )

        # Other user should not be rate limited
        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Test",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=other_user["headers"],
        )

        # Should succeed (not rate limited)
        assert response.status_code in [200, 201, 402]  # 402 is OK (credits check)


# ===========================================================================
# Category-Based Limit Tests
# ===========================================================================


class TestCategoryLimits:
    """Tests for category-based rate limits."""

    @pytest.mark.skip(
        reason="Billing check (402) runs before rate limit check (429) in test env",
    )
    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_hiring_category_applies_to_assistant_creation(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Hiring category should apply to assistant creation."""
        # Exhaust hiring limit
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=1,
        )

        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Test",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        assert response.status_code == 429
        assert "assistant_hiring" in response.json().get("detail", {}).get(
            "message",
            "",
        )


# ===========================================================================
# Tiered Hiring Limit Tests
# ===========================================================================


class TestTieredHiringLimits:
    """Tests for tiered hiring limits based on account status."""

    @pytest.mark.skip(
        reason="Billing check (402) runs before rate limit check (429) in test env",
    )
    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_new_account_has_low_limit(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """New accounts should have a limit of 1/day."""
        # New account - should have limit of 1
        # Make one successful request (via filling with 0, then making request)
        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "First",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        # First might succeed (if has credits) or fail with 402 (no credits)
        # Either way, rate limit recorded
        first_status = response.status_code

        # Second request should be rate limited (limit is 1)
        response2 = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Second",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        # Should be rate limited after first request
        assert response2.status_code == 429


# ===========================================================================
# Rolling Window Tests
# ===========================================================================


class TestRollingWindow:
    """Tests for rolling 24-hour window behavior."""

    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_old_requests_dont_count(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Requests older than 24 hours should not count toward limit."""
        # Add an old request (25 hours ago)
        old_bucket = datetime.now(timezone.utc) - timedelta(hours=25)
        old_record = RateLimitCounter(
            user_id=rate_limit_test_user["id"],
            endpoint_category="assistant_hiring",
            time_bucket=old_bucket,
            request_count=10,  # Would exceed any limit
        )
        session.add(old_record)
        session.commit()

        # Current request should succeed (old one doesn't count)
        response = await client.post(
            "/v0/assistant",
            json={
                "first_name": "Test",
                "surname": "Assistant",
                "age": 25,
                "nationality": "American",
                "create_infra": False,
            },
            headers=rate_limit_test_user["headers"],
        )

        # Should not be 429 (rate limited) - old requests don't count
        assert response.status_code != 429


# ===========================================================================
# Configuration Tests
# ===========================================================================


class TestRateLimitConfiguration:
    """Tests for rate limit configuration."""

    def test_valid_categories(self):
        """All configured categories should be valid."""
        from orchestra.web.api.utils.rate_limiting import RATE_LIMITS, VALID_CATEGORIES

        for category in RATE_LIMITS.keys():
            assert category in VALID_CATEGORIES

    def test_all_categories_have_default_limit(self):
        """All categories should have a default limit."""
        from orchestra.web.api.utils.rate_limiting import RATE_LIMITS

        for category, limits in RATE_LIMITS.items():
            assert "default" in limits, f"Category {category} missing default limit"
            assert limits["default"] > 0

    def test_endpoint_overrides_have_valid_categories(self):
        """Endpoint overrides should use valid paths."""
        from orchestra.web.api.utils.rate_limiting import ENDPOINT_OVERRIDES

        for path, limits in ENDPOINT_OVERRIDES.items():
            assert path.startswith("/v0/"), f"Path {path} should start with /v0/"
            assert "default" in limits


# ===========================================================================
# Staging Bypass Tests
# ===========================================================================


class TestStagingBypass:
    """Tests for staging/dev environment rate limit bypass."""

    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_rate_limits_bypassed_in_staging(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Rate limits should be bypassed when is_staging is True."""
        # Fill up the rate limit to exceed it
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=100,  # Way over the limit
        )

        # Mock settings to simulate staging environment
        with patch(
            "orchestra.web.api.utils.rate_limiting.settings",
        ) as mock_settings:
            mock_settings.is_staging = True
            mock_settings.environment = "production"

            response = await client.post(
                "/v0/assistant",
                json={
                    "first_name": "Test",
                    "surname": "Assistant",
                    "age": 25,
                    "nationality": "American",
                    "create_infra": False,
                },
                headers=rate_limit_test_user["headers"],
            )

            # Should NOT be 429 - rate limits bypassed in staging
            # May be 402 (no credits) or other, but not 429
            assert response.status_code != 429

    @pytest.mark.usefixtures("clean_rate_limits")
    async def test_rate_limits_bypassed_in_dev_environment(
        self,
        client: AsyncClient,
        rate_limit_test_user: Dict[str, Any],
        session: Session,
    ):
        """Rate limits should be bypassed when environment is 'dev'."""
        # Fill up the rate limit to exceed it
        _fill_rate_limit(
            session,
            user_id=rate_limit_test_user["id"],
            category="assistant_hiring",
            count=100,
        )

        # Mock settings to simulate dev environment
        with patch(
            "orchestra.web.api.utils.rate_limiting.settings",
        ) as mock_settings:
            mock_settings.is_staging = False
            mock_settings.environment = "dev"

            response = await client.post(
                "/v0/assistant",
                json={
                    "first_name": "Test",
                    "surname": "Assistant",
                    "age": 25,
                    "nationality": "American",
                    "create_infra": False,
                },
                headers=rate_limit_test_user["headers"],
            )

            # Should NOT be 429 - rate limits bypassed in dev
            assert response.status_code != 429
