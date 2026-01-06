"""Async version of AuthUserDAO for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import AuthUser

ASSISTANT_HIRING_APPROVAL_STATUSES = [
    None,
    "pending",
    "approved",
    "rejected",
    "revoked",
]


class AsyncAuthUserDAO:
    """Async Data Access Object for AuthUser operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
        assistant_hiring_approval: Optional[str] = "__use_default_no_filter__",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List:
        """Filter AuthUser records by various criteria."""
        query = select(AuthUser)
        if id:
            query = query.where(AuthUser.id == id)
        if email:
            query = query.where(AuthUser.email == email)
        if assistant_hiring_approval != "__use_default_no_filter__":
            if assistant_hiring_approval is None:
                query = query.where(AuthUser.assistant_hiring_approval.is_(None))
            else:
                if assistant_hiring_approval not in ASSISTANT_HIRING_APPROVAL_STATUSES:
                    raise ValueError(
                        f"Invalid assistant hiring approval status: {assistant_hiring_approval}",
                    )
                query = query.where(
                    AuthUser.assistant_hiring_approval == assistant_hiring_approval,
                )

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return result.fetchall()

    async def get_by_id(self, user_id: str) -> Optional[AuthUser]:
        """Return a single AuthUser object or None given a user_id."""
        found = await self.filter(id=user_id)
        return found[0] if found else None

    async def get_by_email(self, email: str) -> Optional[AuthUser]:
        """Return a single AuthUser object or None given an email."""
        found = await self.filter(email=email)
        return found[0] if found else None

    async def update(
        self,
        id: str,
        queries_enabled: Optional[bool] = ...,
        onboarded: Optional[bool] = ...,
    ) -> None:
        """Update an AuthUser record with the provided fields."""
        query = select(AuthUser).where(AuthUser.id == id)
        result = await self.session.execute(query)
        entry = result.scalars().first()

        if entry is not None:
            if queries_enabled is not ...:
                entry.queries_enabled = queries_enabled
            if onboarded is not ...:
                entry.onboarded = onboarded
