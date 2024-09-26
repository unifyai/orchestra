from typing import List
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Router


class RouterDAO:
    """Class for accessing trained routers"""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self, user_id: str, router_name: str,
    ) -> None:
        pass

    def filter(self, user_id: Optional[str] = None, name: Optional[str] = None):
        query = select(Router)
        if user_id:
            query = query.where(Router.user_id == user_id)
        if name:
            query = query.where(Router.name == name)
        
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())
    

