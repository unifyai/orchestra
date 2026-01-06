"""Async version of stored_prompt_dao for use with AsyncSession."""

import datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import StoredPrompt


class AsyncStoredPromptDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(  # noqa: WPS211
        self,
        user_id: Optional[str],
        system_msg: Optional[str],
        messages: str,
        prompt_kwargs: str,
        extra_fields: dict,
        num_tokens: int,
        timestamp: datetime.datetime,
    ) -> None:
        self.session.add(
            StoredPrompt(
                user_id=user_id,
                system_msg=system_msg,
                messages=messages,
                prompt_kwargs=prompt_kwargs,
                extra_fields=extra_fields,
                num_tokens=num_tokens,
                timestamp=timestamp,
            ),
        )

    async def delete(
        self,
        id: int,
        user_id: str,
    ) -> Dict[str, str]:
        prompt = (
            (
                await self.session.execute(
                    select(StoredPrompt).filter_by(id=id, user_id=user_id),
                )
            )
            .scalars()
            .one()
        )
        await self.session.delete(prompt)
        await self.session.commit()
        return {"info": "Prompt deleted successfully"}

    async def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        user_id: Optional[str] = None,
        system_msg: Optional[str] = None,
        messages: Optional[str] = None,
    ) -> List[StoredPrompt]:
        query = select(StoredPrompt)
        if id:
            query = query.where(StoredPrompt.id == id)
        if user_id:
            query = query.where(StoredPrompt.user_id == user_id)
        if system_msg:
            query = query.where(StoredPrompt.system_msg == system_msg)
        if messages:
            query = query.where(StoredPrompt.messages == messages)
        rows = await self.session.execute(query)
        return list(rows.scalars().unique().fetchall())

    async def check_ids_valid(self, user_id, datum_ids):
        query = (
            select(StoredPrompt.id)
            .where(StoredPrompt.user_id == user_id)
            .where(StoredPrompt.id.in_(datum_ids))
        )
        matching_ids = await self.session.execute(query).scalars().all()
        invalid_ids = set(datum_ids).difference(set(matching_ids))
        return invalid_ids

    async def get_prompts(self, datum_ids: list, user_id: str):
        query = (
            select(StoredPrompt)
            .where(StoredPrompt.user_id == user_id)
            .where(StoredPrompt.id.in_(datum_ids))
        )
        rows = await self.session.execute(query)
        return list(rows.scalars().unique().fetchall())
