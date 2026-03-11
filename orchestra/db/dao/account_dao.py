import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Account


class AccountDAO:
    def __init__(self, session: Session):
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

    def delete(self, id: str):
        try:
            account = self.session.query(Account).filter_by(id=id).one()
            self.session.delete(account)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
