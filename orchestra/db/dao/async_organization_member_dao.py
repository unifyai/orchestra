"""Async version of organization_member_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import (
    AuthUser,
    Organization,
    OrganizationMember,
)


class AsyncOrganizationMemberDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
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
        await self.session.flush()
        return member

    async def filter(
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
        rows = await self.session.execute(query)
        return rows.fetchall()

    async def update(
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
        raw = await self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)
            if role_id is not None:
                setattr(entry, "role_id", role_id)

    async def list_members(self, name: str):
        from orchestra.db.models.orchestra_models import Role

        query = (
            select(
                AuthUser.email,
                OrganizationMember.role_id,
                Role.name.label("role_name"),
            )
            .join(OrganizationMember, OrganizationMember.user_id == AuthUser.id)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .join(Role, OrganizationMember.role_id == Role.id)
            .where(Organization.name == name)
        )
        raw = await self.session.execute(query)
        entries = [
            {
                "email": entry.email,
                "role_id": entry.role_id,
                "role_name": entry.role_name,
            }
            for entry in raw.fetchall()
        ]
        return entries

    async def get_member(
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

    async def update_member_role(
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
            await self.session.flush()

    async def delete(self, id: int):
        try:
            org_member = (
                (
                    await self.session.execute(
                        select(OrganizationMember).filter_by(id=id),
                    )
                )
                .scalars()
                .one()
            )
            await self.session.delete(org_member)
            await self.session.commit()
        except:
            await self.session.rollback()
            raise ValueError

    async def count_members(self, organization_id: int) -> int:
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
