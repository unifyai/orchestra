from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Interface


class InterfaceDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_interface(self, user_id: str, items: str, new_counter: int):
        self.session.add(
            Interface(user_id=user_id, items=items, new_counter=new_counter),
        )

    def update_interface(self, user_id: str, items: str, new_counter: int):
        query = select(Interface)
        query = query.where(Interface.user_id == user_id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "items", items)  # noqa: B010
            setattr(entry, "new_counter", new_counter)

    def get_interface(self, user_id: str):
        query = select(Interface).where(Interface.user_id == user_id)
        interface = self.session.execute(query).scalars().first()
        return interface
