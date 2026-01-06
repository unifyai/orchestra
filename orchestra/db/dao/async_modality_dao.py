"""Async version of modality_dao for use with AsyncSession."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import Modality


class AsyncModalityDAO:
    """Class for accessing modality table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_modality(
        self,
        name: str,
    ) -> None:
        """
        Add single modality to session.

        :param name: name of a modality.
        """
        self.session.add(
            Modality(
                name=name,
            ),
        )

    async def get_all_modalities(self, limit: int, offset: int) -> List[Modality]:
        """
        Get all modality models with limit/offset pagination.

        :param limit: limit of modalities.
        :param offset: offset of modalities.
        :return: stream of modalities.
        """
        raw_modalities = await self.session.execute(
            select(Modality).limit(limit).offset(offset),
        )

        return list(raw_modalities.scalars().fetchall())

    async def filter(
        self,
        name: Optional[str] = None,
    ) -> List[Modality]:
        """
        Get specific modality model.

        :param name: name of modality instance.
        :return: modality models.
        """
        query = select(Modality)
        if name:
            query = query.where(Modality.name == name)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
