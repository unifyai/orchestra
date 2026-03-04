"""
Consolidated User DAO.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional
from zoneinfo import available_timezones

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, BillingAccount, User
from orchestra.web.api.utils.http_responses import not_found

if TYPE_CHECKING:
    from orchestra.db.dao.account_dao import AccountDAO
    from orchestra.db.dao.api_key_dao import ApiKeyDAO
    from orchestra.db.dao.email_account_dao import EmailAccountDAO
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.role_dao import RoleDAO

VALID_TIMEZONES = available_timezones()


@dataclass
class UserSpendingCapResult:
    """Result of setting a user spending cap with cascade updates."""

    assistants_capped: int = 0


class UserDAO:
    """
    Data Access Object for the unified User table.

    Billing account operations (credits, autorecharge, Stripe, freeze/status,
    auto-recharge eligibility) are handled by BillingAccountDAO.
    This DAO manages user profile fields, spending caps, and telemetry.
    """

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # CREATE / READ / UPDATE / DELETE
    # =========================================================================

    def create(
        self,
        email: str,
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        bio: Optional[str] = None,
        image: Optional[str] = None,
        timezone: Optional[str] = None,
        phone_number: Optional[str] = None,
        credits: float = 0,
    ) -> User:
        """
        Create a new user with an associated BillingAccount.

        :param email: User's email (required, unique).
        :param name: First name.
        :param last_name: Last name.
        :param job_title: Job title.
        :param bio: User bio.
        :param image: Profile image URL.
        :param timezone: IANA timezone string.
        :param phone_number: Phone number (will be validated and formatted).
        :param credits: Initial credit balance (default 0).
        :return: The created User instance.
        """
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

        # Create a BillingAccount for this user
        billing_account = BillingAccount(
            credits=credits,
        )
        self.session.add(billing_account)
        self.session.flush()  # Get the billing_account.id

        user = User(
            email=email,
            name=name,
            last_name=last_name,
            job_title=job_title,
            bio=bio,
            image=image,
            timezone=timezone,
            phone_number=phone_number,
            billing_account_id=billing_account.id,
            store_prompts=True,
        )
        self.session.add(user)
        return user

    def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List:
        """
        Filter users by criteria.

        :param id: Filter by user ID.
        :param email: Filter by email.
        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :return: List of matching users (as Row tuples).
        """
        query = select(User)
        if id:
            query = query.where(User.id == id)
        if email:
            query = query.where(User.email == email)

        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)

        rows = self.session.execute(query)
        return rows.fetchall()

    def get_by_id(self, user_id: str) -> Optional:
        """
        Get a user by ID.

        :param user_id: User ID.
        :return: User row tuple or None.
        """
        found = self.filter(id=user_id)
        return found[0] if found else None

    def get_user_with_id(self, id: str) -> User:
        """
        Get user by ID, raising an error if not found.

        :param id: User ID.
        :return: User instance.
        :raises HTTPException: If user not found.
        """
        query = select(User).where(User.id == id)
        result = self.session.execute(query).scalars().first()
        if result is None:
            raise not_found("User ID")
        return result

    def get_all_users(self) -> List[User]:
        """
        Get all users.

        :return: List of all users.
        """
        raw_users = self.session.execute(select(User))
        return list(raw_users.scalars().fetchall())

    def get_user_by_stripe_id(self, stripe_id: str) -> Optional[User]:
        """
        Get a user by their Stripe customer ID (via BillingAccount).

        :param stripe_id: The Stripe customer ID.
        :return: A User object or None if not found.
        """
        query = (
            select(User)
            .join(BillingAccount, User.billing_account_id == BillingAccount.id)
            .where(BillingAccount.stripe_customer_id == stripe_id)
        )
        result = self.session.execute(query).scalars().first()
        return result

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: str,
        name: Optional[str] = ...,
        last_name: Optional[str] = ...,
        job_title: Optional[str] = ...,
        bio: Optional[str] = ...,
        image: Optional[str] = ...,
        timezone: Optional[str] = ...,
        phone_number: Optional[str] = ...,
        queries_enabled: Optional[bool] = ...,
        evaluations_enabled: Optional[bool] = ...,
        monthly_spending_cap: Optional[float] = ...,
    ) -> None:
        """
        Update user profile fields.

        Uses ellipsis (...) as default to distinguish between
        "not provided" and "set to None".

        Note: tier is now on BillingAccount, not on User.
        """
        query = select(User).where(User.id == id)
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
            if queries_enabled is not ...:
                setattr(entry, "queries_enabled", queries_enabled)
            if evaluations_enabled is not ...:
                setattr(entry, "evaluations_enabled", evaluations_enabled)

            # Handle monthly_spending_cap with cascade logic
            if monthly_spending_cap is not ...:
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

    def delete(self, id: str) -> None:
        """Delete a user by ID."""
        try:
            user = self.session.query(User).filter_by(id=id).one()
            self.session.delete(user)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise ValueError("User not found")

    # =========================================================================
    # TELEMETRY METHODS
    # =========================================================================

    def is_telemetry_activated(self, id: str) -> bool:
        """Check if prompt telemetry is activated for a user."""
        user = self.get_user_with_id(id)
        return user.store_prompts if user.store_prompts is not None else True

    def set_prompt_telemetry(self, user_id: str, activated: bool) -> None:
        """Set prompt telemetry activation status."""
        user = self.get_user_with_id(user_id)
        if user is not None:
            user.store_prompts = activated

    # =========================================================================
    # SPENDING CAP METHODS
    # =========================================================================

    def set_spending_cap(
        self,
        user_id: str,
        monthly_spending_cap: Optional[float],
    ) -> UserSpendingCapResult:
        """
        Set user's personal spending cap with cascade to personal assistants.

        When the limit is lowered, all personal assistant limits are automatically capped.

        :param user_id: User ID.
        :param monthly_spending_cap: New spending cap (None = no limit).
        :return: Result containing count of cascaded updates.
        """
        result = UserSpendingCapResult()

        user_row = self.get_by_id(user_id)
        if not user_row:
            return result

        user = user_row[0]
        old_limit = user.monthly_spending_cap
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

        # Track when the limit value changes
        if old_limit != new_limit:
            from datetime import datetime, timezone

            user.monthly_spending_cap_set_at = datetime.now(timezone.utc)

        return result

    def get_spending_cap(self, user_id: str) -> Optional[float]:
        """Get user's personal monthly spending cap."""
        user_row = self.get_by_id(user_id)
        if user_row:
            user = user_row[0]
            if user.monthly_spending_cap is not None:
                return float(user.monthly_spending_cap)
        return None

    def get_cumulative_spend(self, user_id: str, month: str) -> float:
        """
        Get user's cumulative spend for a given month.

        :param user_id: User ID.
        :param month: Month in YYYY-MM format.
        :return: Cumulative spend for the month.
        """
        from sqlalchemy import cast, func
        from sqlalchemy.types import Float

        from orchestra.db.models.orchestra_models import (
            Context,
            LogEvent,
            LogEventContext,
            Project,
        )

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

    # =========================================================================
    # AUTH PROVIDER HELPERS
    # =========================================================================

    def get_linked_providers(
        self,
        user_id: str,
        account_dao: Optional["AccountDAO"] = None,
        email_account_dao: Optional["EmailAccountDAO"] = None,
    ) -> List[str]:
        """
        Get the list of auth providers linked to a user.

        Returns provider names from OAuth accounts (e.g. "google", "github")
        and "email" if the user has an EmailAccount.

        :param user_id: The user's ID.
        :param account_dao: Optional AccountDAO instance (created from session if omitted).
        :param email_account_dao: Optional EmailAccountDAO instance (created if omitted).
        :return: List of provider name strings.
        """
        from orchestra.db.dao.account_dao import AccountDAO
        from orchestra.db.dao.email_account_dao import EmailAccountDAO

        if account_dao is None:
            account_dao = AccountDAO(self.session)
        if email_account_dao is None:
            email_account_dao = EmailAccountDAO(self.session)

        providers = []
        # Check OAuth providers
        accounts = account_dao.filter(user_id=user_id)
        for row in accounts:
            account = row[0] if hasattr(row, "__getitem__") else row
            providers.append(account.provider)
        # Check email/password
        if email_account_dao.get_by_user_id(user_id):
            providers.append("email")
        return providers

    # =========================================================================
    # ORGANIZATION MEMBERSHIP HELPERS
    # =========================================================================

    def get_user_organizations(
        self,
        user_id: str,
        organization_dao: "OrganizationDAO",
        organization_member_dao: "OrganizationMemberDAO",
        api_key_dao: "ApiKeyDAO",
        role_dao: "RoleDAO",
    ) -> List[dict]:
        """
        Get list of organizations a user belongs to with role and API key details.

        This consolidates the repeated logic for building organization lists
        in user profile endpoints.

        :param user_id: User ID.
        :param organization_dao: OrganizationDAO instance.
        :param organization_member_dao: OrganizationMemberDAO instance.
        :param api_key_dao: ApiKeyDAO instance.
        :param role_dao: RoleDAO instance.
        :return: List of organization dicts with id, name, role_id, role_name, api_key, timezone.
        """

        org_members = organization_member_dao.filter(user_id=user_id)

        organizations = []
        for member_row in org_members:
            member = member_row[0]
            org_result = organization_dao.get(member.organization_id)
            if org_result:
                # Get org-specific API key for this user+org
                org_keys = api_key_dao.get_organization_keys(
                    user_id,
                    organization_id=member.organization_id,
                )
                org_api_key = org_keys[0][0].key if org_keys else None

                # Get role name for this membership
                member_role = role_dao.get(member.role_id)
                member_role_name = member_role.name if member_role else None

                organizations.append(
                    {
                        "id": member.organization_id,
                        "name": org_result.name,
                        "owner_id": org_result.owner_id,
                        "role_id": member.role_id,
                        "role_name": member_role_name,
                        "api_key": org_api_key,
                        "timezone": org_result.timezone,
                    },
                )

        return organizations
