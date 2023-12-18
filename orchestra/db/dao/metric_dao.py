from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Metric


class MetricDAO:
    """Class for accessing metric table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_metric(
        self,
        name: str,
    ) -> None:
        """
        Add single metric to session.

        :param name: name of a metric.
        """
        self.session.add(
            Metric(
                name=name,
            ),
        )

    async def get_all_metrics(self, limit: int, offset: int) -> List[Metric]:
        """
        Get all metric models with limit/offset pagination.

        :param limit: limit of metrics.
        :param offset: offset of metrics.
        :return: stream of metrics.
        """
        raw_metrics = await self.session.execute(
            select(Metric).limit(limit).offset(offset),
        )

        return list(raw_metrics.scalars().fetchall())

    async def filter(
        self,
        name: Optional[str] = None,
    ) -> List[Metric]:
        """
        Get specific metric model.

        :param name: name of metric instance.
        :return: metric models.
        """
        query = select(Metric)
        if name:
            query = query.where(Metric.name == name)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
