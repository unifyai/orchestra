from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional
from zoneinfo import available_timezones

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, AuthUser


@dataclass
class UserSpendingCapResult:
    """Result of setting a user spending cap with cascade updates."""

    assistants_capped: int = 0


ASSISTANT_HIRING_APPROVAL_STATUSES = [
    None,
    "pending",
    "approved",
    "rejected",
    "revoked",
]

VALID_TIMEZONES = available_timezones()


class AuthUserDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(  # noqa: WPS211
        self,
        email: str,
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        bio: Optional[str] = None,
        image: Optional[str] = None,
        timezone: Optional[str] = None,
        phone_number: Optional[str] = None,
        account_type: Optional[str] = None,
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
        if timezone is not None and timezone not in VALID_TIMEZONES:
            raise ValueError(f"'{timezone}' is not a valid IANA timezone.")

        # Validate and format phone_number if provided
        if phone_number is not None:
            from orchestra.web.api.utils.phone_number_validator import (
                validate_phone_number,
            )

            result = validate_phone_number(phone_number)
            if not result["is_valid"]:
                raise ValueError(f"Invalid phone number: {result['error']}")
            phone_number = result["formatted_phone_number"]

        # Validate account_type if provided
        if account_type is not None and account_type not in ["individual", "business"]:
            raise ValueError("account_type must be 'individual' or 'business'")

        # If account_type is business, require business information
        if account_type == "business":
            if not business_name:
                raise ValueError("business_name is required for business accounts")
            if not business_address_line1 or not business_city or not business_country:
                raise ValueError(
                    "Complete business address is required for business accounts",
                )

        self.session.add(
            AuthUser(
                email=email,
                name=name,
                last_name=last_name,
                job_title=job_title,
                bio=bio,
                image=image,
                timezone=timezone,
                phone_number=phone_number,
                account_type=account_type or "individual",
                business_name=business_name,
                tax_id=tax_id,
                business_type=business_type,
                business_address_line1=business_address_line1,
                business_address_line2=business_address_line2,
                business_city=business_city,
                business_state=business_state,
                business_country=business_country,
                business_postal_code=business_postal_code,
                tax_exempt=tax_exempt or False,
                business_verified=False,  # Always start as unverified
                tax_jurisdiction=None,  # Will be set during verification
            ),
        )

    def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
        assistant_hiring_approval: Optional[
            str
        ] = "__use_default_no_filter__",  # Sentinel
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:  # Technically List[RowProxy]
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
                        f"Invalid assistant hiring approval status for filtering: {assistant_hiring_approval}",
                    )
                query = query.where(
                    AuthUser.assistant_hiring_approval == assistant_hiring_approval,
                )

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        rows = self.session.execute(query)
        return rows.fetchall()  # Returns List[RowProxy]

    def get_by_id(
        self,
        user_id: str,
    ) -> Optional[AuthUser]:  # Technically Optional[RowProxy]
        """Return a single AuthUser object or None given a user_id."""
        found = self.filter(id=user_id)
        return found[0] if found else None

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = ...,
        last_name: Optional[str] = ...,
        job_title: Optional[str] = ...,
        bio: Optional[str] = ...,
        image: Optional[str] = ...,
        timezone: Optional[str] = ...,
        phone_number: Optional[str] = ...,
        tier: Optional[str] = ...,
        queries_enabled: Optional[bool] = ...,
        evaluations_enabled: Optional[bool] = ...,
        has_claimed_approval_link: Optional[bool] = ...,
        assistant_hiring_approval: Optional[str] = ...,
        onboarded: Optional[bool] = ...,
        account_type: Optional[str] = ...,
        business_name: Optional[str] = ...,
        tax_id: Optional[str] = ...,
        business_type: Optional[str] = ...,
        business_address_line1: Optional[str] = ...,
        business_address_line2: Optional[str] = ...,
        business_city: Optional[str] = ...,
        business_state: Optional[str] = ...,
        business_country: Optional[str] = ...,
        business_postal_code: Optional[str] = ...,
        tax_exempt: Optional[bool] = ...,
        business_verified: Optional[bool] = ...,
        tax_jurisdiction: Optional[str] = ...,
        monthly_spending_cap: Optional[float] = ...,
    ) -> None:
        query = select(AuthUser)
        query = query.where(AuthUser.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            # Track changes for contact sync
            should_sync_timezone = False
            should_sync_bio = False

            if name is not ...:
                setattr(entry, "name", name)
            if last_name is not ...:
                setattr(entry, "last_name", last_name)
            if job_title is not ...:
                setattr(entry, "job_title", job_title)
            if bio is not ...:
                old_bio = getattr(entry, "bio", None)
                if bio != old_bio:
                    should_sync_bio = True
                setattr(entry, "bio", bio)
            if image is not ...:
                setattr(entry, "image", image)
            if timezone is not ...:
                if timezone is not None and timezone not in VALID_TIMEZONES:
                    raise ValueError(f"'{timezone}' is not a valid IANA timezone.")
                old_timezone = getattr(entry, "timezone", None)
                if timezone != old_timezone:
                    should_sync_timezone = True
                setattr(entry, "timezone", timezone)
            if phone_number is not ...:
                if phone_number is not None:
                    from orchestra.web.api.utils.phone_number_validator import (
                        validate_phone_number,
                    )

                    result = validate_phone_number(phone_number)
                    if not result["is_valid"]:
                        raise ValueError(f"Invalid phone number: {result['error']}")
                    phone_number = result["formatted_phone_number"]
                setattr(entry, "phone_number", phone_number)
            if tier is not ...:
                setattr(entry, "tier", tier)
            if queries_enabled is not ...:
                setattr(entry, "queries_enabled", queries_enabled)
            if evaluations_enabled is not ...:
                setattr(entry, "evaluations_enabled", evaluations_enabled)
            if assistant_hiring_approval is not ...:
                if assistant_hiring_approval not in ASSISTANT_HIRING_APPROVAL_STATUSES:
                    raise ValueError(
                        f"Unsupported hiring approval status: {assistant_hiring_approval}",
                    )
                setattr(entry, "assistant_hiring_approval", assistant_hiring_approval)
            if has_claimed_approval_link is not ...:
                setattr(entry, "has_claimed_approval_link", has_claimed_approval_link)

            if onboarded is not ...:
                setattr(entry, "onboarded", onboarded)

            # Business classification fields
            if account_type is not ...:
                if account_type not in ["individual", "business"]:
                    raise ValueError("account_type must be 'individual' or 'business'")
                setattr(entry, "account_type", account_type)
            if business_name is not ...:
                setattr(entry, "business_name", business_name)
            if tax_id is not ...:
                setattr(entry, "tax_id", tax_id)
            if business_type is not ...:
                setattr(entry, "business_type", business_type)
            if business_address_line1 is not ...:
                setattr(entry, "business_address_line1", business_address_line1)
            if business_address_line2 is not ...:
                setattr(entry, "business_address_line2", business_address_line2)
            if business_city is not ...:
                setattr(entry, "business_city", business_city)
            if business_state is not ...:
                setattr(entry, "business_state", business_state)
            if business_country is not ...:
                setattr(entry, "business_country", business_country)
            if business_postal_code is not ...:
                setattr(entry, "business_postal_code", business_postal_code)
            if tax_exempt is not ...:
                setattr(entry, "tax_exempt", tax_exempt)
            if business_verified is not ...:
                setattr(entry, "business_verified", business_verified)
            if tax_jurisdiction is not ...:
                setattr(entry, "tax_jurisdiction", tax_jurisdiction)

            # Handle monthly_spending_cap with cascade logic
            if monthly_spending_cap is not ...:
                # Use set_spending_cap which handles cascading to assistants
                self.set_spending_cap(str(id), monthly_spending_cap)

            self.session.commit()

            # Sync timezone/bio changes to Contact logs in Assistants projects
            if should_sync_timezone or should_sync_bio:
                from orchestra.services.contact_sync_service import ContactSyncService

                sync_service = ContactSyncService(self.session)
                if should_sync_timezone:
                    sync_service.sync_user_timezone(
                        user_id=id,
                        email=entry.email,
                        new_timezone=entry.timezone,
                    )
                if should_sync_bio:
                    sync_service.sync_user_bio(
                        user_id=id,
                        email=entry.email,
                        new_bio=entry.bio,
                    )
                self.session.commit()

    def delete(self, id: str):
        try:
            auth_user = self.session.query(AuthUser).filter_by(id=id).one()
            self.session.delete(auth_user)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    # -- Handle assistant hiring approval --
    def set_assistant_hiring_approval(
        self,
        user_id: str,
        status: Optional[str],
    ) -> bool:
        """Sets the assistant hiring approval status for a user."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES:
            raise ValueError(f"Invalid assistant hiring approval status: {status}")

        user_row = self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            auth_user_instance.assistant_hiring_approval = status
            return True
        return False

    def get_assistant_hiring_approval(self, user_id: str) -> Optional[str]:
        """Gets the assistant hiring approval status for a user."""
        user_row = self.get_by_id(user_id)
        if user_row:
            auth_user_instance = user_row[0]
            return auth_user_instance.assistant_hiring_approval
        return None

    def get_users_by_assistant_hiring_approval(
        self,
        status: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuthUser]:
        """Returns users matching a specific hiring status (e.g., "pending")."""
        if status not in ASSISTANT_HIRING_APPROVAL_STATUSES or status is None:
            raise ValueError(
                "Unsupported or invalid asssistant hiring approval status for querying list.",
            )
        query = select(AuthUser).where(AuthUser.assistant_hiring_approval == status)

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        return list(self.session.execute(query).scalars().all())

    # -- Business Classification Methods --

    def update_account_type(
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

        user_row = self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        auth_user.account_type = account_type

        # If switching to business, require business information
        if account_type == "business":
            if not business_name:
                raise ValueError("business_name is required for business accounts")
            if not business_address_line1 or not business_city or not business_country:
                raise ValueError(
                    "Complete business address is required for business accounts",
                )

        # Update business fields (will be None for individual accounts)
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

        # Reset verification status when business info changes
        auth_user.business_verified = False

        self.session.commit()

    def update_business_info(
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
        user_row = self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        if auth_user.account_type != "business":
            raise ValueError("Can only update business info for business accounts")

        # Update provided fields
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

        # Reset verification status when business info changes
        auth_user.business_verified = False

        self.session.commit()

    def set_business_verified(
        self,
        user_id: str,
        verified: bool,
        tax_jurisdiction: Optional[str] = None,
    ) -> None:
        """Set business verification status."""
        user_row = self.get_by_id(user_id)
        if not user_row:
            raise ValueError(f"User with id {user_id} not found")

        auth_user = user_row[0]
        if auth_user.account_type != "business":
            raise ValueError("Can only verify business accounts")

        auth_user.business_verified = verified
        if tax_jurisdiction is not None:
            auth_user.tax_jurisdiction = tax_jurisdiction

        self.session.commit()

    def get_users_by_account_type(
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

        return list(self.session.execute(query).scalars().all())

    def get_business_users_by_verification_status(
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

        return list(self.session.execute(query).scalars().all())

    def set_spending_cap(
        self,
        user_id: str,
        monthly_spending_cap: Optional[float],
    ) -> UserSpendingCapResult:
        """
        Set user's personal spending cap with cascade to personal assistants.

        When the limit is lowered, all personal assistant limits (organization_id=NULL)
        that exceed the new limit are automatically capped.

        :param user_id: User ID.
        :param monthly_spending_cap: New spending cap (None = no limit).
        :return: Result containing count of cascaded updates.
        """
        result = UserSpendingCapResult()

        user_row = self.get_by_id(user_id)
        if not user_row:
            return result

        user = user_row[0]
        new_limit = (
            Decimal(str(monthly_spending_cap))
            if monthly_spending_cap is not None
            else None
        )

        # If lowering the limit, cap personal assistant limits
        if new_limit is not None:
            assistants_to_cap = (
                self.session.query(Assistant)
                .filter(
                    Assistant.user_id == user_id,
                    Assistant.organization_id.is_(None),
                    Assistant.monthly_spending_cap > new_limit,
                )
                .all()
            )
            for assistant in assistants_to_cap:
                assistant.monthly_spending_cap = new_limit
                result.assistants_capped += 1

        user.monthly_spending_cap = new_limit
        return result

    def get_spending_cap(self, user_id: str) -> Optional[float]:
        """
        Get user's personal monthly spending cap.

        :param user_id: User ID.
        :return: Monthly spending cap or None if not set or user not found.
        """
        user_row = self.get_by_id(user_id)
        if user_row:
            user = user_row[0]
            if user.monthly_spending_cap is not None:
                return float(user.monthly_spending_cap)
        return None

    def get_cumulative_spend(self, user_id: str, month: str) -> float:
        """
        Get user's cumulative spend for a given month (personal context).

        Queries the user's personal Assistants project logs for spending data.
        Aggregates cumulative_spend across all assistants owned by this user.

        :param user_id: User ID.
        :param month: Month in YYYY-MM format.
        :return: Cumulative spend for the month (0.0 if no spend data).
        """
        from sqlalchemy import cast, func
        from sqlalchemy.types import Float

        from orchestra.db.models.orchestra_models import (
            Context,
            LogEvent,
            LogEventContext,
            Project,
        )

        # Sum cumulative_spend across all assistants for this user
        # Logs are stored in All/Spending/Monthly with _user_id field
        result = (
            self.session.query(
                func.coalesce(
                    func.sum(cast(LogEvent.data.op("->>")("cumulative_spend"), Float)),
                    0.0,
                ).label("spend"),
            )
            .select_from(LogEvent)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .join(Context, LogEventContext.context_id == Context.id)
            .join(Project, Context.project_id == Project.id)
            .filter(
                Project.name == "Assistants",
                Project.user_id == user_id,
                Project.organization_id.is_(None),
                Context.name == "All/Spending/Monthly",
                LogEvent.data.op("->>")("_user_id") == user_id,
                LogEvent.data.op("->>")("month") == month,
            )
            .first()
        )

        if result and result.spend:
            return float(result.spend)
        return 0.0
