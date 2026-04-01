"""Rate limiting configuration and FastAPI dependency.

This module provides:
- Rate limit configuration (categories, limits, per-endpoint overrides)
- FastAPI dependency for checking and recording rate limits
- Helper functions for calculating tiered hiring limits

This replaces the previous approval-based gating system with a more
flexible rate limiting approach.

Note: Rate limits are bypassed in staging and dev environments to
allow for testing without restrictions.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.rate_limit_counter_dao import RateLimitCounterDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Organization, User
from orchestra.settings import settings

# =============================================================================
# Rate Limit Configuration
# =============================================================================

# Default limits per category (requests per 24-hour rolling window)
# Format: {"default": limit, "verified": limit_for_verifieds}
# Category names are prefixed with "assistant_" to indicate they're for the assistant API
RATE_LIMITS = {
    "assistant_hiring": {
        # Hiring uses tiered limits based on account status (see get_hiring_limit)
        # These are fallback values
        "default": 1,
        "established": 5,
        "verified": 10,
    },
    "assistant_media": {
        "default": 20,
        "verified": 40,
    },
    "assistant_crud": {
        "default": 100,
        "verified": 200,
    },
    "assistant_voice": {
        "default": 50,
        "verified": 100,
    },
}

# Per-endpoint overrides (optional)
# If an endpoint is listed here, its limit takes precedence over the category limit
ENDPOINT_OVERRIDES = {
    "/v0/assistant/photo/generate": {"default": 10, "verified": 20},
    "/v0/assistant/photo/animate": {"default": 5, "verified": 10},
    "/v0/assistant/voice/clone": {"default": 10, "verified": 20},
}

# Thresholds for "established" account status
ESTABLISHED_ACCOUNT_AGE_DAYS = 30
ESTABLISHED_ACCOUNT_MIN_SPEND = 50.0  # USD

# Valid categories (must match check constraint in migration)
VALID_CATEGORIES = {
    "assistant_hiring",
    "assistant_media",
    "assistant_crud",
    "assistant_voice",
}


# =============================================================================
# Helper Functions
# =============================================================================


def get_assistant_hiring_limit(
    session: Session,
    user_id: str,
    organization_id: Optional[int] = None,
) -> int:
    """
    Get the assistant hiring rate limit based on account status.

    Tiered limits:
    - New account (< 30 days OR < $50 spent): 1/day
    - Established account (>= 30 days AND >= $50 spent): 5/day
    - Verified organization: 10/day

    Args:
        session: Database session
        user_id: ID of the user
        organization_id: Optional organization ID for org context

    Returns:
        Daily hiring limit
    """
    # Check for verified organization first
    if organization_id is not None:
        org = (
            session.query(Organization)
            .filter(Organization.id == organization_id)
            .first()
        )
        if org and getattr(org, "verified", False):
            return RATE_LIMITS["assistant_hiring"]["verified"]

    # Check user account status
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        return RATE_LIMITS["assistant_hiring"]["default"]

    # Calculate account age
    now = datetime.now(timezone.utc)
    created_at = user.created_at
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    account_age_days = (now - created_at).days if created_at else 0

    from orchestra.db.models.orchestra_models import RECHARGE_TYPE_PROMO, Recharge

    total_spend = 0.0
    ba_id = session.query(User.billing_account_id).filter(User.id == user_id).scalar()
    if ba_id is not None:
        recharges = (
            session.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba_id,
                Recharge.type != RECHARGE_TYPE_PROMO,
            )
            .all()
        )
        total_spend = sum(
            float(r.quantity) for r in recharges if r.quantity and r.quantity > 0
        )

    # Check if account is established
    if (
        account_age_days >= ESTABLISHED_ACCOUNT_AGE_DAYS
        and total_spend >= ESTABLISHED_ACCOUNT_MIN_SPEND
    ):
        return RATE_LIMITS["assistant_hiring"]["established"]

    return RATE_LIMITS["assistant_hiring"]["default"]


def get_rate_limit(
    session: Session,
    user_id: str,
    category: str,
    endpoint_path: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> int:
    """
    Get the rate limit for a specific category/endpoint.

    Checks in order:
    1. Per-endpoint override (if endpoint_path provided and has override)
    2. Category limit (with verified org bonus if applicable)

    Args:
        session: Database session
        user_id: ID of the user
        category: Rate limit category
        endpoint_path: Optional specific endpoint path
        organization_id: Optional organization ID

    Returns:
        Rate limit (requests per 24 hours)
    """
    # Special handling for assistant hiring (tiered based on account status)
    if category == "assistant_hiring":
        return get_assistant_hiring_limit(session, user_id, organization_id)

    # Check for endpoint-specific override
    if endpoint_path and endpoint_path in ENDPOINT_OVERRIDES:
        override = ENDPOINT_OVERRIDES[endpoint_path]
        # Check for verified org
        if organization_id is not None:
            org = (
                session.query(Organization)
                .filter(Organization.id == organization_id)
                .first()
            )
            if org and getattr(org, "verified", False):
                return override.get("verified", override["default"])
        return override["default"]

    # Use category limit
    if category not in RATE_LIMITS:
        # Unknown category - use a conservative default
        return 10

    category_limits = RATE_LIMITS[category]

    # Check for verified org bonus
    if organization_id is not None:
        org = (
            session.query(Organization)
            .filter(Organization.id == organization_id)
            .first()
        )
        if org and getattr(org, "verified", False):
            return category_limits.get("verified", category_limits["default"])

    return category_limits["default"]


# =============================================================================
# FastAPI Dependency
# =============================================================================


def check_rate_limit(
    category: str,
    use_org_shared_limit: bool = True,
) -> Callable:
    """
    Create a FastAPI dependency that checks rate limits.

    It checks the rate limit, returns 429 if exceeded, and records the request.

    Args:
        category: Rate limit category ('assistant_hiring', 'assistant_media',
                  'assistant_crud', 'assistant_voice')
        use_org_shared_limit: If True, org members share the org's limit

    Returns:
        FastAPI dependency function

    Usage:
        @router.post("/assistant")
        async def create_assistant(
            ...,
            _: None = Depends(check_rate_limit("assistant_hiring")),
        ):
            ...
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid rate limit category: {category}")

    def dependency(
        request: Request,
        session: Session = Depends(get_db_session),
    ) -> None:
        # Bypass rate limits in staging and dev environments
        if settings.is_staging or settings.environment == "dev":
            return

        user_id = request.state.user_id
        organization_id = getattr(request.state, "organization_id", None)
        endpoint_path = request.url.path

        # Verify user exists
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Authenticated user not found.",
            )

        # Get the rate limit
        limit = get_rate_limit(
            session=session,
            user_id=user_id,
            category=category,
            endpoint_path=endpoint_path,
            organization_id=organization_id,
        )

        # Get current usage
        rate_limit_dao = RateLimitCounterDAO(session)
        usage = rate_limit_dao.get_request_count_24h(
            user_id=user_id,
            endpoint_category=category,
            organization_id=organization_id,
            use_org_limit=use_org_shared_limit and organization_id is not None,
        )

        # Check if limit exceeded
        if usage >= limit:
            reset_seconds = rate_limit_dao.get_seconds_until_reset(
                user_id=user_id,
                endpoint_category=category,
                organization_id=organization_id,
                use_org_limit=use_org_shared_limit and organization_id is not None,
            )
            reset_time = datetime.now(timezone.utc) + timedelta(seconds=reset_seconds)

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "message": f"Rate limit exceeded for {category}",
                    "limit": limit,
                    "used": usage,
                    "reset_in_seconds": reset_seconds,
                    "reset_at": reset_time.isoformat(),
                },
            )

        # Record this request
        rate_limit_dao.record_request(
            user_id=user_id,
            endpoint_category=category,
            endpoint_path=endpoint_path,
            organization_id=organization_id,
        )

        # Commit the rate limit record
        session.commit()

    return dependency


# =============================================================================
# Convenience Dependencies (pre-configured for assistant API categories)
# =============================================================================

# Pre-configured dependencies for assistant API rate limiting
check_assistant_hiring_rate_limit = check_rate_limit("assistant_hiring")
check_assistant_media_rate_limit = check_rate_limit("assistant_media")
check_assistant_crud_rate_limit = check_rate_limit("assistant_crud")
check_assistant_voice_rate_limit = check_rate_limit("assistant_voice")
