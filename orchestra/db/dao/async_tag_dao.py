"""Async version of tag_dao for use with AsyncSession."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Tag


class AsyncTagDAO:
    """Class for accessing tag table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all_tags(self, user_id):
        query = select(Tag)
        query = query.where(Tag.user_id == user_id)
        rows = await self.session.execute(query)
        tag_data = list(rows.scalars().fetchall())
        return [t.tag_name for t in tag_data]
