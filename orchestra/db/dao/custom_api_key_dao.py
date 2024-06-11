import copy
from typing import List, Optional
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomApiKey


class CustomApiKeyDAO:
    """Class for accessing custom api key table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_custom_api_key(self, user_id: str, key: str, value: str) -> None:
        self.session.add(CustomApiKey(user_id=user_id, key=key, value=value))

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        key: Optional[str] = None,
    ) -> List[CustomApiKey]:
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
        query = select(CustomApiKey).where(CustomApiKey.user_id == user_id)

        raw_custom_api_keys = self.session.execute(query)
        fetched = list(raw_custom_api_keys.scalars().fetchall())
        copied = copy.deepcopy(fetched)
        for cak in copied:
            cak.value = f"****{cak.value[-4:]}"
        return copied
