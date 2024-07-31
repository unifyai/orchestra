import os
from typing import List, Tuple

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomApiKey, CustomEndpoint
from orchestra.web.api.utils.on_prem import OnPremModel


class CustomEndpointDAO:
    """Class for accessing custom endpoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session
        self.on_prem = os.environ.get("ON_PREM")
        if self.on_prem:
            self.on_prem_model = OnPremModel(
                model_class=CustomEndpoint,
                table_name="custom_endpoint",
            )

    def create_custom_endpoint(
        self,
        user_id: str,
        name: str,
        url: str,
        key_id: int,
    ) -> None:
        data = {"user_id": user_id, "name": name, "url": url, "key_id": key_id}
        if self.on_prem:
            self.on_prem_model.create(**data)
        else:
            self.session.add(CustomEndpoint(**data))

    def filter(self, user_id: str, name: str) -> List[CustomEndpoint]:
        if self.on_prem:
            return self.on_prem_model.read(
                filters={
                    "custom_endpoint": {"user_id": user_id, "name": name},
                },
            )
        query = (
            select(CustomEndpoint)
            .where(CustomEndpoint.user_id == user_id)
            .where(CustomEndpoint.name == name)
        )
        raw_custom_endpoints = self.session.execute(query)
        return list(raw_custom_endpoints.scalars().fetchall())

    def get_user_endpoints(self, user_id: str) -> List[Tuple[str, str, str]]:
        if self.on_prem:
            return self.on_prem_model.read(
                filters={"custom_endpoint": {"user_id": user_id}},
                join_table="custom_api_key",
                join_columns=["key_id", "id"],
                select_columns={
                    "custom_endpoint": ["name", "url"],
                    "custom_api_key": ["key"],
                },
            )
        query = (
            select(CustomEndpoint.name, CustomEndpoint.url, CustomApiKey.key)
            .join(CustomApiKey, CustomEndpoint.key_id == CustomApiKey.id)
            .where(CustomEndpoint.user_id == user_id)
        )
        raw_custom_endpoints = self.session.execute(query)
        return list(raw_custom_endpoints.fetchall())
