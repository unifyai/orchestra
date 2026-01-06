"""Async version of UsersDAO for use with AsyncSession."""

import decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Query, Users
from orchestra.web.api.utils.http_responses import not_found

# Constants for billing requirements
MIN_SPEND_FOR_MONTHLY_BILLING = 100.0  # $100 in credits (100 credits)
MIN_AUTORECHARGE_AMOUNT = 25.0  # $25 in credits (25 credits)


class AsyncUsersDAO:
    """Async Data Access Object for Users operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def filter(self, id: Optional[str]) -> List[Users]:
        """Get specific users model."""
        query = select(Users).where(Users.id == id)
        result = await self.session.execute(query)
        return list(result.scalars().fetchall())

    async def get_user_with_id(self, id: str) -> Users:
        """Get user by ID or raise 404."""
        users = await self.filter(id=id)
        if not users:
            raise not_found("User ID")
        return users[0]

    async def get_user_by_stripe_id(self, stripe_id: str) -> Optional[Users]:
        """Get a user by their Stripe customer ID."""
        query = select(Users).where(Users.stripe_customer_id == stripe_id)
        result = await self.session.execute(query)
        return result.scalars().first()

    async def get_total_spending(self, user_id: str) -> float:
        """Calculate total spending for a user from the Query table."""
        result = await self.session.execute(
            select(func.sum(Query.credits)).where(Query.user_id == user_id),
        )
        total = result.scalar()
        return float(total) if total else 0.0

    async def can_enable_monthly_billing(self, user_id: str) -> bool:
        """Check if user has spent enough to enable monthly billing."""
        total_spending = await self.get_total_spending(user_id)
        return total_spending >= MIN_SPEND_FOR_MONTHLY_BILLING

    async def recharge_credit(self, user_id: str, quantity: float) -> None:
        """Recharge (or deduct if negative) credits for a user."""
        query = select(Users).where(Users.id == user_id)
        result = await self.session.execute(query)
        user = result.scalars().first()
        if user is not None:
            new_credits = user.credits + decimal.Decimal(quantity)
            user.credits = new_credits

    async def is_account_frozen(self, user_id: str) -> bool:
        """Check if a user account is frozen."""
        try:
            user = await self.get_user_with_id(user_id)
            return user.frozen
        except Exception:
            return False
