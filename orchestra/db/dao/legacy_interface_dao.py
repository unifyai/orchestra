from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Interface


class LegacyInterfaceDAO:
    def __init__(self, session: Session):
        self.session = session

    def create_interface(
        self,
        name: str,
        items: str,
        new_counter: int,
        project_id: int,
        context: str | None = None,
        color: str | None = None,
        icon: str | None = "folder",
        order: int | None = None,
    ):
        self.session.add(
            Interface(
                name=name,
                items=items,
                new_counter=new_counter,
                project_id=project_id,
                context=context,
                color=color,
                icon=icon,
                order=order,
            ),
        )
        self.session.commit()

    def update_interface(
        self,
        name: str,
        project_id: int,
        items: str,
        new_counter: int,
        context: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        order: int | None = None,
        new_name: str = None,
    ):
        query = select(Interface)
        query = query.where(Interface.project_id == project_id).where(
            Interface.name == name,
        )
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            setattr(entry, "items", items)  # noqa: B010
            setattr(entry, "new_counter", new_counter)
            setattr(entry, "project_id", project_id)
            if new_name is not None:
                setattr(entry, "name", new_name)
            setattr(entry, "context", context)
            setattr(entry, "color", color)
            if icon is not None:
                setattr(entry, "icon", icon)
            if order is not None:
                setattr(entry, "order", order)

    def get_interfaces(
        self,
        project_id: int = None,
        name: str = None,
    ) -> list[Interface]:
        query = select(Interface)
        if project_id is not None:
            query = query.where(Interface.project_id == project_id)
        if name is not None:
            query = query.where(Interface.name == name)
        query = query.order_by(Interface.created_at.asc())
        interfaces = self.session.execute(query).scalars().all()
        return interfaces

    def delete_interface(self, project_id: int, name: str):
        try:
            interface = (
                self.session.query(Interface)
                .filter(
                    Interface.project_id == project_id,
                    Interface.name == name,
                )
                .first()
            )
            self.session.delete(interface)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
