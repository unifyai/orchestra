import os
from typing import List, Tuple

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import LocalEndpoint


class LocalEndpointDAO:
    """Class for accessing local endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def get_or_create_local_endpoint(
        self,
        user_id: str,
        name: str,
    ) -> int:

        # try and find the endpoint
        stmt = select(LocalEndpoint.id).where(
            LocalEndpoint.user_id == user_id, LocalEndpoint.name == name
        )
        endpoint = list(self.session.execute(stmt).fetchall())
        if endpoint:
            return endpoint[0].id

        # add if not
        try:
            stmt = insert(LocalEndpoint).values(user_id=user_id, name=name)
            stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "name"])
            result = self.session.execute(stmt)
            self.session.commit()

            existing_stmt = select(LocalEndpoint.id).where(
                LocalEndpoint.user_id == user_id, LocalEndpoint.name == name
            )
            return self.session.execute(existing_stmt).scalar_one()
        except:
            raise ValueError

    def get_user_local_endpoints(self, user_id):
        query = select(LocalEndpoint.name).where(LocalEndpoint.user_id == user_id)
        raw_local_endpoints = self.session.execute(query)
        return list(raw_local_endpoints.fetchall())

    def filter(self, user_id, name):
        query = (
            select(LocalEndpoint)
            .where(LocalEndpoint.user_id == user_id)
            .where(LocalEndpoint.name == name)
        )
        raw_custom_endpoints = self.session.execute(query)
        return list(raw_custom_endpoints.scalars().fetchall())
