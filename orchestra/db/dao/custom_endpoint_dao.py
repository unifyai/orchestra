from typing import List, Tuple

from fastapi import Depends
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomApiKey, CustomEndpoint


class CustomEndpointDAO:
    """Class for accessing custom endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_custom_endpoint(
        self,
        user_id: str,
        name: str,
        mdl_name: str,
        url: str,
        key_id: int,
    ) -> None:
        self.session.add(
            CustomEndpoint(
                user_id=user_id,
                name=name,
                mdl_name=mdl_name,
                url=url,
                key_id=key_id,
            ),
        )

    def filter(self, user_id: str, name: str) -> List[CustomEndpoint]:
        query = (
            select(CustomEndpoint)
            .where(CustomEndpoint.user_id == user_id)
            .where(CustomEndpoint.name == name)
        )
        raw_custom_endpoints = self.session.execute(query)
        return list(raw_custom_endpoints.scalars().fetchall())

    def get_user_endpoints(self, user_id: str) -> List[Tuple[str, str, str, str]]:
        query = (
            select(
                CustomEndpoint.name,
                CustomEndpoint.mdl_name,
                CustomEndpoint.url,
                CustomApiKey.key,
            )
            .join(CustomApiKey, CustomEndpoint.key_id == CustomApiKey.id)
            .where(CustomEndpoint.user_id == user_id)
        )
        raw_custom_endpoints = self.session.execute(query)
        return list(raw_custom_endpoints.fetchall())

    def rename(self, user_id: str, name: str, new_name: str):
        query = select(CustomEndpoint)
        query = query.where(CustomEndpoint.user_id == user_id)
        query = query.where(CustomEndpoint.name == name)

        raw_custom_endpoints = self.session.execute(query)
        custom_endpoint = raw_custom_endpoints.scalars().first()
        if custom_endpoint is not None:
            setattr(custom_endpoint, "name", new_name)

    def delete(self, user_id: str, name: str):
        query = delete(CustomEndpoint).where(
            and_(CustomEndpoint.user_id == user_id, CustomEndpoint.name == name),
        )
        self.session.execute(query)
