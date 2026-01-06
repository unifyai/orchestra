"""Async version of account_dao for use with AsyncSession."""

import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Account


class AsyncAccountDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(  # noqa: WPS211
        self,
        user_id: str,
        provider: str,
        provider_type: str,
        provider_account_id: str,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime.datetime] = None,
    ) -> None:
        self.session.add(
            Account(
                user_id=user_id,
                provider=provider,
                provider_type=provider_type,
                provider_account_id=provider_account_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
            ),
        )

    async def filter(
        self,
        id: Optional[str] = None,
        user_id: Optional[str] = None,
        provider: Optional[str] = None,
        provider_account_id: Optional[str] = None,
    ) -> List[Account]:
        query = select(Account)
        if id:
            query = query.where(Account.id == id)
        if user_id:
            query = query.where(Account.user_id == user_id)
        if provider:
            query = query.where(Account.provider == provider)
        if provider_account_id:
            query = query.where(Account.provider_account_id == provider_account_id)
        rows = await self.session.execute(query)
        return rows.fetchall()

    async def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        # query = select(DefaultPrompt)
        # query = query.where(DefaultPrompt.id == id)
        # raw = await self.session.execute(query)
        # entry = raw.scalars().first()
        # if entry is not None:
        #     if name:
        #         setattr(entry, "name", name)  # noqa: B010
        pass

    async def delete(self, id: str):
        try:
            account = (
                (await self.session.execute(select(Account).filter_by(id=id)))
                .scalars()
                .one()
            )
            await self.session.delete(account)
            await self.session.commit()
        except:
            await self.session.rollback()
            raise ValueError
