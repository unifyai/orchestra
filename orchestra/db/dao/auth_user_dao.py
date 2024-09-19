from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import AuthUser


class AuthUserDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        email: str,
    ) -> None:
        self.session.add(AuthUser(email=email))

    def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
    ) -> List[AuthUser]:
        query = select(AuthUser)
        if id:
            query = query.where(AuthUser.id == id)
        if email:
            query = query.where(AuthUser.email == email)
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
            auth_user = self.session.query(AuthUser).filter_by(id=id).one()
            self.session.delete(auth_user)
        except:
            self.session.rollback()
            raise ValueError
