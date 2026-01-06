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

    async def set_assistant_hiring_approval(
        self,
        user_id: str,
        status: Optional[str],
    ) -> bool:
        """Sets the assistant hiring approval status for a user."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES:
            raise ValueError(f"Invalid assistant hiring approval status: {status}")

        user_row = await self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            auth_user_instance.assistant_hiring_approval = status
            return True
        return False

    async def get_assistant_hiring_approval(self, user_id: str) -> Optional[str]:
        """Gets the assistant hiring approval status for a user."""
        user_row = await self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            return auth_user_instance.assistant_hiring_approval
        return None

    async def get_users_by_assistant_hiring_approval(
        self,
        status: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:
        """Returns users matching a specific hiring status (e.g., "pending")."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES or status is None:
            raise ValueError(
                "Unsupported or invalid assistant hiring approval status for querying list.",
            )
        query = select(AuthUser).where(AuthUser.assistant_hiring_approval == status)

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_account_type(
        self,
        user_id: str,
        account_type: str,
        business_name: Optional[str] = None,
        tax_id: Optional[str] = None,
        business_type: Optional[str] = None,
        business_address_line1: Optional[str] = None,
        business_address_line2: Optional[str] = None,
        business_city: Optional[str] = None,
        business_state: Optional[str] = None,
        business_country: Optional[str] = None,
        business_postal_code: Optional[str] = None,
        tax_exempt: Optional[bool] = None,
    ) -> None:
        """Update user account type and business information."""
        if account_type not in ["individual", "business"]:
            raise ValueError("account_type must be 'individual' or 'business'")

        user_row = await self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        auth_user.account_type = account_type

        if account_type == "business":
            if not business_name:
                raise ValueError("business_name is required for business accounts")
            if not business_address_line1 or not business_city or not business_country:
                raise ValueError(
                    "Complete business address is required for business accounts",
                )

        auth_user.business_name = business_name
        auth_user.tax_id = tax_id
        auth_user.business_type = business_type
        auth_user.business_address_line1 = business_address_line1
        auth_user.business_address_line2 = business_address_line2
        auth_user.business_city = business_city
        auth_user.business_state = business_state
        auth_user.business_country = business_country
        auth_user.business_postal_code = business_postal_code
        auth_user.tax_exempt = tax_exempt or False
        auth_user.business_verified = False

        await self.session.commit()

    async def update_business_info(
        self,
        user_id: str,
        business_name: Optional[str] = None,
        tax_id: Optional[str] = None,
        business_type: Optional[str] = None,
        business_address_line1: Optional[str] = None,
        business_address_line2: Optional[str] = None,
        business_city: Optional[str] = None,
        business_state: Optional[str] = None,
        business_country: Optional[str] = None,
        business_postal_code: Optional[str] = None,
        tax_exempt: Optional[bool] = None,
    ) -> None:
        """Update business information for a user (must be business account)."""
        user_row = await self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        if auth_user.account_type != "business":
            raise ValueError("Can only update business info for business accounts")

        if business_name is not None:
            auth_user.business_name = business_name
        if tax_id is not None:
            auth_user.tax_id = tax_id
        if business_type is not None:
            auth_user.business_type = business_type
        if business_address_line1 is not None:
            auth_user.business_address_line1 = business_address_line1
        if business_address_line2 is not None:
            auth_user.business_address_line2 = business_address_line2
        if business_city is not None:
            auth_user.business_city = business_city
        if business_state is not None:
            auth_user.business_state = business_state
        if business_country is not None:
            auth_user.business_country = business_country
        if business_postal_code is not None:
            auth_user.business_postal_code = business_postal_code
        if tax_exempt is not None:
            auth_user.tax_exempt = tax_exempt

        auth_user.business_verified = False
        await self.session.commit()

    async def set_business_verified(
        self,
        user_id: str,
        verified: bool,
        tax_jurisdiction: Optional[str] = None,
    ) -> None:
        """Set business verification status."""
        user_row = await self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        if auth_user.account_type != "business":
            raise ValueError("Can only verify business accounts")

        auth_user.business_verified = verified
        if tax_jurisdiction is not None:
            auth_user.tax_jurisdiction = tax_jurisdiction

        await self.session.commit()

    async def get_users_by_account_type(
        self,
        account_type: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:
        """Get users by account type (individual or business)."""
        if account_type not in ["individual", "business"]:
            raise ValueError("account_type must be 'individual' or 'business'")

        query = select(AuthUser).where(AuthUser.account_type == account_type)

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_business_users_by_verification_status(
        self,
        verified: bool,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:
        """Get business users by verification status."""
        query = select(AuthUser).where(
            AuthUser.account_type == "business",
            AuthUser.business_verified == verified,
        )

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())
