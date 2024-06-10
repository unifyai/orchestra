from typing import List
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomEndpoint


class CustomEndpointDAO:
    """Class for accessing custom endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_custom_endpoint(self, user_id: str, key: str, value: str) -> None:
        self.session.add(CustomEndpoint(user_id=user_id, key=key, value=value))

    def filter(self, user_id: str, name: str) -> List[CustomEndpoint]:
        query = (
            select(CustomEndpoint)
            .where(CustomEndpoint.user_id == user_id)
            .where(CustomEndpoint.name == name)
        )

        raw_custom_endpoints = self.session.execute(query)

        return list(raw_custom_endpoints.scalars().fetchall())
