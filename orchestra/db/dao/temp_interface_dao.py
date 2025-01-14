from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import TempInterface


class TempInterfaceDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_interface(
        self,
        user_id: str,
        items: str,
        new_counter: int,
        project: str | None,
    ):
        self.session.add(
            TempInterface(
                user_id=user_id,
                items=items,
                new_counter=new_counter,
                project=project,
            ),
        )

    def update_interface(
        self,
        user_id: str,
        items: str,
        new_counter: int,
        project: str | None,
    ):
        query = select(TempInterface)
        query = query.where(TempInterface.user_id == user_id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "items", items)  # noqa: B010
            setattr(entry, "new_counter", new_counter)
            setattr(entry, "project", project)

    def get_interface(self, user_id: str):
        query = select(TempInterface).where(TempInterface.user_id == user_id)
        interface = self.session.execute(query).fetchall()
        return interface[0][0] if len(interface) else None

    def delete_interface(self, user_id: str):
        try:
            interface = (
                self.session.query(TempInterface).filter_by(user_id=user_id).one()
            )
            self.session.delete(interface)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
