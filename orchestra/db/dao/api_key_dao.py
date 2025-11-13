from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import ApiKey, AuthUser


class ApiKeyDAO:
    def __init__(self, session: Session):
        self.session = session

    def create(  # noqa: WPS211
        self,
        key: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self.session.add(
            ApiKey(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
                key=key,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        key: Optional[str] = None,
    ) -> List[ApiKey]:
        query = select(ApiKey)
        if id:
            query = query.where(ApiKey.id == id)
        if user_id:
            query = query.where(ApiKey.user_id == user_id)
        if organization_id:
            query = query.where(ApiKey.organization_id == organization_id)
        if key:
            query = query.where(ApiKey.key == key)
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_user_id_and_mail(self, key):
        query = select(
            ApiKey.user_id,
            AuthUser.email,
            AuthUser.name,
            AuthUser.last_name,
        )
        query = query.join(AuthUser, ApiKey.user_id == AuthUser.id)
        query = query.where(ApiKey.key == key)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> None:
        query = select(ApiKey)
        query = query.where(ApiKey.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)

    def delete(self, id: int):
        try:
            api_key = self.session.query(ApiKey).filter_by(id=id).one()
            self.session.delete(api_key)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

    def get_personal_keys(self, user_id: str) -> List[ApiKey]:
        """
        Get all personal API keys for a user (organization_id is NULL).

        :param user_id: User ID to filter by.
        :return: List of personal API keys.
        """
        query = select(ApiKey)
        query = query.where(
            ApiKey.user_id == user_id,
            ApiKey.organization_id.is_(None),
        )
        rows = self.session.execute(query)
        return rows.fetchall()

    def get_organization_keys(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
    ) -> List[ApiKey]:
        """
        Get organization API keys for a user.

        If organization_id is provided, returns keys for that specific organization.
        If organization_id is None, returns all organization keys for the user.

        :param user_id: User ID to filter by.
        :param organization_id: Optional organization ID to filter by.
        :return: List of organization API keys.
        """
        query = select(ApiKey)
        query = query.where(
            ApiKey.user_id == user_id,
            ApiKey.organization_id.is_not(None),
        )
        if organization_id is not None:
            query = query.where(ApiKey.organization_id == organization_id)
        rows = self.session.execute(query)
        return rows.fetchall()

    def revoke_organization_keys(
        self,
        user_id: str,
        organization_id: int,
    ) -> int:
        """
        Revoke (delete) all organization API keys for a user in a specific organization.
        This is used when removing a user from an organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :return: Number of keys revoked.
        """
        keys = self.filter(user_id=user_id, organization_id=organization_id)
        count = 0
        for key_row in keys:
            api_key = key_row[0]
            self.session.delete(api_key)
            count += 1
        return count
