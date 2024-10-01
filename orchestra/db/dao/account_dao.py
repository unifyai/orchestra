import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Account


class AccountDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
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

    def filter(
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
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
    ) -> None:
        # query = select(DefaultPrompt)
        # query = query.where(DefaultPrompt.id == id)
        # raw = self.session.execute(query)
        # entry = raw.scalars().first()
        # if entry is not None:
        #     if name:
        #         setattr(entry, "name", name)  # noqa: B010
        pass

    def delete(self, id: str):
        try:
            account = self.session.query(Account).filter_by(id=id).one()
            self.session.delete(account)
        except:
            self.session.rollback()
            raise ValueError
