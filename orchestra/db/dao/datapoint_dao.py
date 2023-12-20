import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Datapoint


class DatapointDAO:
    """Class for accessing datapoint table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_datapoint(
        self,
        endpoint_id: int,
        measured_at: datetime.datetime,
        metric_name: str,
        value: float,
    ) -> None:
        """
        Add single datapoint to session.

        :param endpoint_id: endpoint_id of a datapoint.
        :param measured_at: measured_at of a datapoint.
        :param metric_name: metric_name of a datapoint.
        :param value: value of a datapoint.
        """
        self.session.add(
            Datapoint(
                endpoint_id=endpoint_id,
                measured_at=measured_at,
                metric_name=metric_name,
                value=value,
            ),
        )

    async def get_all_datapoints(self, limit: int, offset: int) -> List[Datapoint]:
        """
        Get all datapoint models with limit/offset pagination.

        :param limit: limit of datapoints.
        :param offset: offset of datapoints.
        :return: stream of datapoints.
        """
        raw_datapoints = await self.session.execute(
            select(Datapoint).limit(limit).offset(offset),
        )

        return list(raw_datapoints.scalars().fetchall())

    async def filter(
        self,
        endpoint_id: Optional[int] = None,
        measured_at: Optional[datetime.datetime] = None,
        metric_name: Optional[str] = None,
        value: Optional[float] = None,
    ) -> List[Datapoint]:
        """
        Get specific datapoint model.

        :param endpoint_id: endpoint_id of datapoint instance.
        :param measured_at: measured_at of datapoint instance.
        :param metric_name: metric_name of datapoint instance.
        :param value: value of datapoint instance.
        :return: datapoint models.
        """
        query = select(Datapoint)
        if endpoint_id:
            query = query.where(Datapoint.endpoint_id == endpoint_id)
        if measured_at:
            query = query.where(Datapoint.measured_at == measured_at)
        if metric_name:
            query = query.where(Datapoint.metric_name == metric_name)
        if value:
            query = query.where(Datapoint.value == value)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
