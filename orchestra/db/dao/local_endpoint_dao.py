import os
from typing import List, Tuple

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import LocalEndpoint


class LocalEndpointDAO:
    """Class for accessing local endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def get_local_endpoint(
        self,
        user_id: str,
        name: str,
    ) -> int:
        data = {
            "user_id": user_id,
            "name": name,
        }
        new_endpoint = LocalEndpoint(**data)
        try:
            self.session.add(new_endpoint)
            self.session.flush()
            endpoint_id = new_endpoint.id
        except:
            self.session.rollback()
            stmt = select(LocalEndpoint).where(
                LocalEndpoint.user_id == user_id, LocalEndpoint.name == name
            )
            existing_endpoint = self.session.execute(stmt).scalar_one_or_none()
            if existing_endpoint:
                endpoint_id = existing_endpoint.id
            else:
                raise ValueError("Failed to create or retrieve local endpoint")
        else:
            self.session.commit()

        return endpoint_id

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
