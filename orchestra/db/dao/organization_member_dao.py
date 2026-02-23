from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrganizationMember,
    User,
)


@dataclass
class MemberSpendingCapResult:
    """Result of setting a member spending cap with cascade updates."""

    assistants_capped: int = 0


class OrganizationMemberDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        organization_id: int,
        user_id: str,
        role_id: int,
    ) -> OrganizationMember:
        """
        Create an organization member.

        :param organization_id: Organization ID.
        :param user_id: User ID.
        :param role_id: RBAC role ID (Owner, Admin, Member, Viewer, or custom role).
        :return: Created OrganizationMember object.
        """
        member = OrganizationMember(
            user_id=user_id,
            organization_id=organization_id,
            role_id=role_id,
        )
        self.session.add(member)
        self.session.flush()
        return member

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        role_id: Optional[int] = None,
    ) -> List[OrganizationMember]:
        query = select(OrganizationMember)
        if id:
            query = query.where(OrganizationMember.id == id)
        if user_id:
            query = query.where(OrganizationMember.user_id == user_id)
        if organization_id:
            query = query.where(OrganizationMember.organization_id == organization_id)
        if role_id:
            query = query.where(OrganizationMember.role_id == role_id)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        role_id: Optional[int] = None,
    ) -> None:
        """
        Update an organization member.

        :param id: Member ID.
        :param user_id: New user ID.
        :param organization_id: New organization ID.
        :param role_id: New RBAC role ID.
        """
        query = select(OrganizationMember)
        query = query.where(OrganizationMember.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)
            if role_id is not None:
                setattr(entry, "role_id", role_id)

    def list_members(self, name: str):
        from orchestra.db.models.orchestra_models import Role

        query = (
            select(
                User.email,
                OrganizationMember.role_id,
                Role.name.label("role_name"),
            )
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .join(Role, OrganizationMember.role_id == Role.id)
            .where(Organization.name == name)
        )
        raw = self.session.execute(query)
        entries = [
            {
                "email": entry.email,
                "role_id": entry.role_id,
                "role_name": entry.role_name,
            }
            for entry in raw.fetchall()
        ]
        return entries

    def get_member(
        self,
        user_id: str,
        organization_id: int,
    ) -> Optional[OrganizationMember]:
        """
        Get a specific organization member.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :return: OrganizationMember object or None if not found.
        """
        return (
            self.session.query(OrganizationMember)
            .filter_by(user_id=user_id, organization_id=organization_id)
            .first()
        )

    def update_member_role(
        self,
        user_id: str,
        organization_id: int,
        role_id: int,
    ) -> None:
        """
        Update a member's RBAC role.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :param role_id: New role ID.
        """
        member = self.get_member(user_id, organization_id)
        if member:
            member.role_id = role_id
            self.session.flush()

    def delete(self, id: int):
        try:
            org_member = self.session.query(OrganizationMember).filter_by(id=id).one()
            self.session.delete(org_member)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def count_members(self, organization_id: int) -> int:
        """
        Count the number of members in an organization.

        :param organization_id: Organization ID.
        :return: Number of members in the organization.
        """
        from sqlalchemy import func

        result = (
            self.session.query(func.count(OrganizationMember.id))
            .filter(OrganizationMember.organization_id == organization_id)
            .scalar()
        )
        return result or 0

    def set_spending_cap(
        self,
        user_id: str,
        organization_id: int,
        monthly_spending_cap: Optional[float],
        org_spending_cap: Optional[float] = None,
    ) -> MemberSpendingCapResult:
        """
        Set member spending cap with validation and cascade to assistants.

        Validates that the member limit does not exceed the org limit.
        When the limit is lowered, all assistant limits owned by this member
        that exceed the new limit are automatically capped.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :param monthly_spending_cap: New spending cap (None = no limit).
        :param org_spending_cap: Organization's spending cap for validation.
        :return: Result containing count of cascaded updates.
        :raises ValueError: If member limit exceeds org limit.
        """
        result = MemberSpendingCapResult()

        member = self.get_member(user_id, organization_id)
        if not member:
            return result

        # Validate against org limit
        if monthly_spending_cap is not None and org_spending_cap is not None:
            if monthly_spending_cap > org_spending_cap:
                raise ValueError(
                    f"Member limit cannot exceed organization limit (${org_spending_cap:.2f})",
                )

        old_limit = member.monthly_spending_cap
        new_limit = (
            Decimal(str(monthly_spending_cap))
            if monthly_spending_cap is not None
            else None
        )

        # If lowering the limit, cap assistant limits owned by this member in this org
        if new_limit is not None:
            assistants_to_cap = (
                self.session.query(Assistant)
                .filter(
                    Assistant.organization_id == organization_id,
                    Assistant.user_id == user_id,
                    Assistant.monthly_spending_cap > new_limit,
                )
                .all()
            )
            for assistant in assistants_to_cap:
                assistant.monthly_spending_cap = new_limit
                result.assistants_capped += 1

        member.monthly_spending_cap = new_limit

        # Track when the limit value changes (for notification deduplication)
        if old_limit != new_limit:
            from datetime import datetime, timezone

            member.monthly_spending_cap_set_at = datetime.now(timezone.utc)

        return result

    def get_spending_cap(
        self,
        user_id: str,
        organization_id: int,
    ) -> Optional[float]:
        """
        Get a member's monthly spending cap.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :return: Monthly spending cap or None if not set or member not found.
        """
        member = self.get_member(user_id, organization_id)
        if member and member.monthly_spending_cap is not None:
            return float(member.monthly_spending_cap)
        return None

    def get_cumulative_spend(
        self,
        user_id: str,
        organization_id: int,
        month: str,
    ) -> float:
        """
        Get a member's cumulative spend for a given month within an organization.

        Sums all assistant spending logs for this user in the organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
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
                    func.sum(cast(LogEvent.data.op("->>")("cumulative_spend"), Float)),
                    0.0,
                ).label("total_spend"),
            )
            .select_from(LogEvent)
            .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
            .join(Context, LogEventContext.context_id == Context.id)
            .join(Project, Context.project_id == Project.id)
            .filter(
                Project.name == "Assistants",
                Project.organization_id == organization_id,
                Context.name == "All/Spending/Monthly",
                LogEvent.data.op("->>")("_user_id") == user_id,
                LogEvent.data.op("->>")("month") == month,
            )
            .first()
        )

        if result and result.total_spend:
            return float(result.total_spend)
        return 0.0
