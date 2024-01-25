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

    async def create_metric(  # noqa: WPS211
        self,
        name: str,
        units: str,
        display_name: str,
        tooltip: Optional[str] = None,
        priority: int = 0,
        plottable: bool = False,
    ) -> None:
        """
        Add single metric to session.

        :param name: name of a metric.
        :param units: units of a metric.
        :param display_name: display_name of a metric.
        :param tooltip: tooltip of a metric.
        :param priority: priority of a metric.
        :param plottable: plottable of a metric.
        """
        self.session.add(
            Metric(
                name=name,
                units=units,
                display_name=display_name,
                tooltip=tooltip,
                priority=priority,
                plottable=plottable,
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

    async def filter(  # noqa: WPS211, C901
        self,
        name: Optional[str] = None,  # noqa: WPS125
        units: Optional[str] = None,
        display_name: Optional[str] = None,
        tooltip: Optional[str] = None,
        priority: Optional[int] = None,
        plottable: Optional[bool] = None,
    ) -> List[Metric]:
        """
        Filter metrics by given parameters.

        :param name: name of a metric.
        :param units: units of a metric.
        :param display_name: display_name of a metric.
        :param tooltip: tooltip of a metric.
        :param priority: priority of a metric.
        :param plottable: plottable of a metric.
        :return: metrics models.
        """
        query = select(Metric)
        if name:
            query = query.where(Metric.name == name)
        if units:
            query = query.where(Metric.units == units)
        if display_name:
            query = query.where(Metric.display_name == display_name)
        if tooltip:
            query = query.where(Metric.tooltip == tooltip)
        if priority:
            query = query.where(Metric.priority == priority)
        if plottable is not None:
            query = query.where(Metric.plottable == plottable)
        raw_metrics = await self.session.execute(query)
        return list(raw_metrics.scalars().fetchall())
