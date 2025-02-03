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
        name: str,
        items: str,
        new_counter: int,
        project: str,
        context: str | None,
    ):
        self.session.add(
            TempInterface(
                user_id=user_id,
                name=name,
                items=items,
                new_counter=new_counter,
                project=project,
                context=context,
            ),
        )

    def update_interface(
        self,
        user_id: str,
        name: str,
        project: str,
        context: str | None,
        items: str,
        new_counter: int,
        new_name: str = None,
    ):
        query = select(TempInterface)
        query = (
            query.where(TempInterface.user_id == user_id)
            .where(TempInterface.project == project)
            .where(TempInterface.name == name)
        )
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "items", items)  # noqa: B010
            setattr(entry, "new_counter", new_counter)
            setattr(entry, "project", project)
            setattr(entry, "context", context)
            if new_name is not None:
                setattr(entry, "name", new_name)

    def get_interfaces(
        self,
        user_id: str,
        project: str = None,
        name: str = None,
    ) -> list[TempInterface]:
        query = select(TempInterface).where(TempInterface.user_id == user_id)
        if project is not None:
            query = query.where(TempInterface.project == project)
        if name is not None:
            query = query.where(TempInterface.name == name)
        interfaces = self.session.execute(query).fetchall()
        return [interface[0] for interface in interfaces]

    def delete_interface(self, user_id: str, name: str):
        try:
            interface = (
                self.session.query(TempInterface)
                .filter_by(
                    user_id=user_id,
                    name=name,
                )
                .first()
            )
            self.session.delete(interface)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
