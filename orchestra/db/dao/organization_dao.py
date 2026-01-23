from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrganizationMember,
)


@dataclass
class OrgSpendingCapResult:
    """Result of setting an organization spending cap with cascade updates."""

    members_capped: int = 0
    assistants_capped: int = 0


class OrganizationDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(  # noqa: WPS211
        self,
        name: str,
        owner_id: str,
        billing_user_id: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> Organization:
        """
        Create a new organization.

        :param name: Organization name.
        :param owner_id: ID of the user who owns the organization.
        :param billing_user_id: ID of the user who will be billed. Defaults to owner_id.
        :param timezone: IANA timezone string (e.g., "America/New_York"). Defaults to None.
        :return: The created Organization object.
        """
        # Default billing user to owner if not specified
        if billing_user_id is None:
            billing_user_id = owner_id

        org = Organization(
            name=name,
            owner_id=owner_id,
            billing_user_id=billing_user_id,
            timezone=timezone,
        )
        self.session.add(org)
        self.session.flush()  # Flush to get the org ID
        return org

    def filter(
        self,
        id: Optional[int] = None,
        owner_id: Optional[str] = None,
        billing_user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Organization]:
        """
        Filter organizations by various criteria.

        :param id: Organization ID.
        :param owner_id: Owner user ID.
        :param billing_user_id: Billing user ID.
        :param name: Organization name.
        :return: List of matching organizations.
        """
        query = select(Organization)
        if id:
            query = query.where(Organization.id == id)
        if owner_id:
            query = query.where(Organization.owner_id == owner_id)
        if billing_user_id:
            query = query.where(Organization.billing_user_id == billing_user_id)
        if name:
            query = query.where(Organization.name == name)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        owner_id: Optional[str] = None,
        billing_user_id: Optional[str] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        monthly_spending_cap: Optional[float] = None,
    ) -> None:
        """
        Update an organization.

        :param id: Organization ID.
        :param owner_id: New owner user ID.
        :param billing_user_id: New billing user ID.
        :param name: New organization name.
        :param timezone: IANA timezone string (e.g., "America/New_York").
        :param monthly_spending_cap: Monthly spending limit in dollars.
        """
        query = select(Organization)
        query = query.where(Organization.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if owner_id:
                setattr(entry, "owner_id", owner_id)
            if billing_user_id:
                setattr(entry, "billing_user_id", billing_user_id)
            if timezone is not None:
                setattr(entry, "timezone", timezone)
            # Use set_spending_cap which handles cascading to members/assistants
            if monthly_spending_cap is not None:
                self.set_spending_cap(id, monthly_spending_cap)

    def delete(self, id: int):
        """
        Delete an organization and all its associated data.

        :param id: Organization ID.
        :raises ValueError: If the organization doesn't exist or deletion fails.
        """
        try:
            org = self.session.query(Organization).filter_by(id=id).one()
            self.session.delete(org)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def get(self, id: int) -> Optional[Organization]:
        """
        Get an organization by ID.

        :param id: Organization ID.
        :return: Organization object or None if not found.
        """
        return self.session.query(Organization).filter_by(id=id).first()

    def get_billing_user_id(self, organization_id: int) -> Optional[str]:
        """
        Get the billing user ID for an organization.

        :param organization_id: Organization ID.
        :return: Billing user ID or None if organization not found.
        """
        org = self.get(organization_id)
        return org.billing_user_id if org else None

    def get_user_organizations(self, user_id: str) -> List[Organization]:
        """
        Get all organizations a user is a member of or owns.

        :param user_id: User ID.
        :return: List of organizations.
        """
        # Get orgs where user is owner
        owned_orgs = list(
            self.session.query(Organization)
            .filter(Organization.owner_id == user_id)
            .all(),
        )

        # Get orgs where user is a member
        member_orgs = list(
            self.session.query(Organization)
            .join(OrganizationMember)
            .filter(OrganizationMember.user_id == user_id)
            .filter(Organization.owner_id != user_id)  # Exclude owned orgs
            .all(),
        )

        return owned_orgs + member_orgs

    def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
        name_filter: Optional[str] = None,
    ) -> List[Organization]:
        """
        List all organizations with pagination.

        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :param name_filter: Optional partial name match filter.
        :return: List of organizations.
        """
        query = select(Organization)
        if name_filter:
            query = query.where(Organization.name.ilike(f"%{name_filter}%"))
        query = query.order_by(Organization.id).limit(limit).offset(offset)
        return list(self.session.execute(query).scalars().all())

    def set_spending_cap(
        self,
        org_id: int,
        monthly_spending_cap: Optional[float],
    ) -> OrgSpendingCapResult:
        """
        Set organization spending cap and cascade to members/assistants.

        When the limit is lowered, all member limits and assistant limits
        that exceed the new org limit are automatically capped.

        :param org_id: Organization ID.
        :param monthly_spending_cap: New spending cap (None = no limit).
        :return: Result containing counts of cascaded updates.
        """
        result = OrgSpendingCapResult()

        # Update the organization's spending limit
        org = self.get(org_id)
        if not org:
            return result

        if monthly_spending_cap is not None:
            new_limit = Decimal(str(monthly_spending_cap))

            # Cap member limits that exceed the new org limit
            members_to_cap = (
                self.session.query(OrganizationMember)
                .filter(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.monthly_spending_cap > new_limit,
                )
                .all()
            )
            for member in members_to_cap:
                member.monthly_spending_cap = new_limit
                result.members_capped += 1

            # Cap assistant limits that exceed the new org limit
            assistants_to_cap = (
                self.session.query(Assistant)
                .filter(
                    Assistant.organization_id == org_id,
                    Assistant.monthly_spending_cap > new_limit,
                )
                .all()
            )
            for assistant in assistants_to_cap:
                assistant.monthly_spending_cap = new_limit
                result.assistants_capped += 1

            org.monthly_spending_cap = new_limit
        else:
            org.monthly_spending_cap = None

        return result

    def get_spending_cap(self, org_id: int) -> Optional[float]:
        """
        Get organization's monthly spending cap.

        :param org_id: Organization ID.
        :return: Monthly spending cap or None if not set or org not found.
        """
        org = self.get(org_id)
        if org and org.monthly_spending_cap is not None:
            return float(org.monthly_spending_cap)
        return None

    def get_cumulative_spend(self, org_id: int, month: str) -> float:
        """
        Get organization's cumulative spend for a given month.

        Queries the organization's Assistants project logs for spending data.

        :param org_id: Organization ID.
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

        result = (
            self.session.query(
                func.coalesce(
                    cast(LogEvent.data.op("->>")("cumulative_spend"), Float),
                    0.0,
                ).label("spend"),
            )
            .select_from(LogEvent)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .join(Context, LogEventContext.context_id == Context.id)
            .join(Project, Context.project_id == Project.id)
            .filter(
                Project.name == "Assistants",
                Project.organization_id == org_id,
                Context.name == "All/Spending/Monthly",
                LogEvent.data.op("->>")("month") == month,
            )
            .first()
        )

        if result and result.spend:
            return float(result.spend)
        return 0.0
