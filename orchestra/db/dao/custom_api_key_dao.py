from typing import List
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

    def filter(self, id: int) -> List[CustomApiKey]:
        query = select(CustomApiKey).where(CustomApiKey.id == id)

        raw_custom_api_keys = self.session.execute(query)

        return list(raw_custom_api_keys.scalars().fetchall())
