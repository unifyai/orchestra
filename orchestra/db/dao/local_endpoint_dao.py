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

    def create_local_endpoint(
        self,
        user_id: str,
        name: str,
    ) -> None:
        data = {
            "user_id": user_id,
            "name": name,
        }
        new_endpoint = LocalEndpoint(**data)
        try:
            self.session.add(new_endpoint)
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
        else:
            self.session.commit()

    def get_user_local_endpoints(self, user_id):
        query = select(LocalEndpoint.name).where(LocalEndpoint.user_id == user_id)
        raw_local_endpoints = self.session.execute(query)
        return list(raw_local_endpoints.fetchall())
