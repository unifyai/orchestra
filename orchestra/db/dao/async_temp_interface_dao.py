"""Async version of temp_interface_dao for use with AsyncSession."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import TempInterface


class AsyncTempInterfaceDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_interface(
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
            TempInterface(
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
        await self.session.commit()

    async def update_interface(
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
        query = select(TempInterface)
        query = query.where(TempInterface.project_id == project_id).where(
            TempInterface.name == name,
        )
        raw = await self.session.execute(query)
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

    async def get_interfaces(
        self,
        project_id: int = None,
        name: str = None,
    ) -> list[TempInterface]:
        query = select(TempInterface)
        if project_id is not None:
            query = query.where(TempInterface.project_id == project_id)
        if name is not None:
            query = query.where(TempInterface.name == name)
        query = query.order_by(TempInterface.created_at.asc())
        interfaces = await self.session.execute(query).scalars().all()
        return interfaces

    async def delete_interface(self, project_id: int, name: str):
        try:
            interface = (
                self.session.query(TempInterface)
                .filter(
                    TempInterface.project_id == project_id,
                    TempInterface.name == name,
                )
                .first()
            )
            await self.session.delete(interface)
            await self.session.commit()
        except:
            await self.session.rollback()
            raise ValueError
