"""Async version of custom_api_key_dao for use with AsyncSession."""

import copy
from typing import List, Optional

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CustomApiKey


class AsyncCustomApiKeyDAO:
    """Class for accessing custom api key table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_custom_api_key(self, user_id: str, key: str, value: str) -> None:
        self.session.add(
            CustomApiKey(
                user_id=user_id,
                key=key,
                value=value,
            ),
        )

    async def filter(
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

        raw_custom_api_keys = await self.session.execute(query)

        return list(raw_custom_api_keys.scalars().fetchall())

    async def get_user_keys(self, user_id: str) -> List[CustomApiKey]:
        query = select(CustomApiKey).where(CustomApiKey.user_id == user_id)
        raw_custom_api_keys = await self.session.execute(query)
        fetched = list(raw_custom_api_keys.scalars().fetchall())
        copied = copy.deepcopy(fetched)
        for cak in copied:
            cak.value = f"****{cak.value[-4:]}"
        return copied

    async def rename(self, user_id: str, name: str, new_name: str):
        query = select(CustomApiKey)
        query = query.where(CustomApiKey.user_id == user_id)
        query = query.where(CustomApiKey.key == name)

        raw_custom_api_keys = await self.session.execute(query)
        custom_api_key = raw_custom_api_keys.scalars().first()
        if custom_api_key is not None:
            setattr(custom_api_key, "key", new_name)

    async def delete(self, user_id: str, name: str):
        query = delete(CustomApiKey).where(
            and_(CustomApiKey.user_id == user_id, CustomApiKey.key == name),
        )
        await self.session.execute(query)
