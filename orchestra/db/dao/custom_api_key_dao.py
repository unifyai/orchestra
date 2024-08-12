import copy
import os
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomApiKey
from orchestra.web.api.utils.on_prem import OnPremModel


class CustomApiKeyDAO:
    """Class for accessing custom api key table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session
        self.on_prem = os.environ.get("ON_PREM")
        if self.on_prem:
            self.on_prem_model = OnPremModel(
                model_class=CustomApiKey,
                table_name="custom_api_key",
            )

    def create_custom_api_key(self, user_id: str, key: str, value: str) -> None:
        data = {"user_id": user_id, "key": key, "value": value}
        if self.on_prem:
            self.on_prem_model.create(**data)
        else:
            self.session.add(CustomApiKey(**data))

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        key: Optional[str] = None,
    ) -> List[CustomApiKey]:
        if self.on_prem:
            return self.on_prem_model.read(
                filters={"custom_api_key": {"id": id, "user_id": user_id, "key": key}},
            )
        query = select(CustomApiKey)
        if id:
            query = query.where(CustomApiKey.id == id)
        if user_id:
            query = query.where(CustomApiKey.user_id == user_id)
        if key:
            query = query.where(CustomApiKey.key == key)

        raw_custom_api_keys = self.session.execute(query)

        return list(raw_custom_api_keys.scalars().fetchall())

    def get_user_keys(self, user_id: str) -> List[CustomApiKey]:
        if self.on_prem:
            fetched = self.on_prem_model.read(
                filters={"custom_api_key": {"user_id": user_id}},
            )
        else:
            query = select(CustomApiKey).where(CustomApiKey.user_id == user_id)
            raw_custom_api_keys = self.session.execute(query)
            fetched = list(raw_custom_api_keys.scalars().fetchall())
        copied = copy.deepcopy(fetched)
        for cak in copied:
            cak.value = f"****{cak.value[-4:]}"
        return copied

    def rename(self, user_id: str, name: str, new_name: str):
        query = select(CustomApiKey)
        query = query.where(CustomApiKey.user_id == user_id)
        query = query.where(CustomApiKey.key == name)

        raw_custom_api_keys = self.session.execute(query)
        custom_api_key = raw_custom_api_keys.scalars().first()
        if custom_api_key is not None:
            setattr(custom_api_key, "key", new_name)

    def delete(self, user_id: str, name: str):
        query = delete(CustomApiKey).where(
            and_(CustomApiKey.user_id == user_id, CustomApiKey.key == name),
        )
        self.session.execute(query)
