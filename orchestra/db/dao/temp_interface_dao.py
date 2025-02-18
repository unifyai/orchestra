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
        project_id: int,
        context: str | None = None,
        column_context: str | None = None,
    ):
        self.session.add(
            TempInterface(
                user_id=user_id,
                name=name,
                items=items,
                new_counter=new_counter,
                project_id=project_id,
                context=context,
                column_context=column_context,
            ),
        )
        self.session.commit()

    def update_interface(
        self,
        user_id: str,
        name: str,
        project_id: int,
        items: str,
        new_counter: int,
        new_name: str = None,
        context: str | None = None,
        column_context: str | None = None,
    ):
        query = select(TempInterface)
        query = (
            query.where(TempInterface.user_id == user_id)
            .where(TempInterface.project_id == project_id)
            .where(TempInterface.name == name)
        )
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "items", items)  # noqa: B010
            setattr(entry, "new_counter", new_counter)
            setattr(entry, "project_id", project_id)
            if new_name is not None:
                setattr(entry, "name", new_name)
            if context is not None:
                setattr(entry, "context", context)
            if column_context is not None:
                setattr(entry, "column_context", column_context)

    def get_interfaces(
        self,
        user_id: str,
        project_id: int = None,
        name: str = None,
    ) -> list[TempInterface]:
        query = select(TempInterface).where(TempInterface.user_id == user_id)
        if project_id is not None:
            query = query.where(TempInterface.project_id == project_id)
        if name is not None:
            query = query.where(TempInterface.name == name)
        interfaces = self.session.execute(query).scalars().all()
        return interfaces

    def delete_interface(self, user_id: str, project_id: int, name: str):
        try:
            interface = (
                self.session.query(TempInterface)
                .filter(
                    TempInterface.user_id == user_id,
                    TempInterface.project_id == project_id,
                    TempInterface.name == name,
                )
                .first()
            )
            self.session.delete(interface)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
