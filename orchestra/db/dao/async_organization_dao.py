"""Async version of organization_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Organization, OrganizationMember


class AsyncOrganizationDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(  # noqa: WPS211
        self,
        name: str,
        owner_id: str,
        billing_user_id: Optional[str] = None,
    ) -> Organization:
        """
        Create a new organization.

        :param name: Organization name.
        :param owner_id: ID of the user who owns the organization.
        :param billing_user_id: ID of the user who will be billed. Defaults to owner_id.
        :return: The created Organization object.
        """
        # Default billing user to owner if not specified
        if billing_user_id is None:
            billing_user_id = owner_id

        org = Organization(
            name=name,
            owner_id=owner_id,
            billing_user_id=billing_user_id,
        )
        self.session.add(org)
        await self.session.flush()  # Flush to get the org ID
        return org

    async def filter(
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
        rows = await self.session.execute(query)
        return rows.fetchall()

    async def update(
        self,
        id: int,
        owner_id: Optional[str] = None,
        billing_user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        """
        Update an organization.

        :param id: Organization ID.
        :param owner_id: New owner user ID.
        :param billing_user_id: New billing user ID.
        :param name: New organization name.
        """
        query = select(Organization)
        query = query.where(Organization.id == id)
        raw = await self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if owner_id:
                setattr(entry, "owner_id", owner_id)
            if billing_user_id:
                setattr(entry, "billing_user_id", billing_user_id)

    async def delete(self, id: int):
        """
        Delete an organization and all its associated data.

        :param id: Organization ID.
        :raises ValueError: If the organization doesn't exist or deletion fails.
        """
        try:
            org = (
                (await self.session.execute(select(Organization).filter_by(id=id)))
                .scalars()
                .one()
            )
            await self.session.delete(org)
            await self.session.commit()
        except:
            await self.session.rollback()
            raise ValueError

    async def get(self, id: int) -> Optional[Organization]:
        """
        Get an organization by ID.

        :param id: Organization ID.
        :return: Organization object or None if not found.
        """
        return (
            (await self.session.execute(select(Organization).filter_by(id=id)))
            .scalars()
            .first()
        )

    async def get_billing_user_id(self, organization_id: int) -> Optional[str]:
        """
        Get the billing user ID for an organization.

        :param organization_id: Organization ID.
        :return: Billing user ID or None if organization not found.
        """
        org = self.get(organization_id)
        return org.billing_user_id if org else None

    async def get_user_organizations(self, user_id: str) -> List[Organization]:
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

    async def list_all(
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
        return list(await self.session.execute(query).scalars().all())
