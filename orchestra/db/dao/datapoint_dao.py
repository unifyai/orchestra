import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Datapoint


class DatapointDAO:
    """Class for accessing datapoint table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_datapoint(  # noqa: WPS211
        self,
        benchmark_run_id: int,
        metric_name: str,
        value: float,
        tooltip: str,
        measured_at: datetime.datetime,
    ) -> None:
        """
        Add single datapoint to session.

        :param benchmark_run_id: benchmark_run_id of a datapoint.
        :param metric_name: metric_name of a datapoint.
        :param value: value of a datapoint.
        :param tooltip: tooltip of a datapoint.
        :param measured_at: measured_at of a datapoint.

        """
        self.session.add(
            Datapoint(
                benchmark_run_id=benchmark_run_id,
                metric_name=metric_name,
                value=value,
                tooltip=tooltip,
                measured_at=measured_at,
            ),
        )

    def get_all_datapoints(self, limit: int, offset: int) -> List[Datapoint]:
        """
        Get all datapoint models with limit/offset pagination.

        :param limit: limit of datapoints.
        :param offset: offset of datapoints.
        :return: stream of datapoints.
        """
        raw_datapoints = self.session.execute(
            select(Datapoint).limit(limit).offset(offset),
        )

        return list(raw_datapoints.scalars().fetchall())

    def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        benchmark_run_id: Optional[int] = None,
        metric_name: Optional[str] = None,
        value: Optional[float] = None,
        tooltip: Optional[str] = None,
        measured_at: Optional[datetime.datetime] = None,
    ) -> List[Datapoint]:
        """
        Get specific datapoint model.

        :param id: id of datapoint instance.
        :param benchmark_run_id: benchmark_run_id of datapoint instance.
        :param metric_name: metric_name of datapoint instance.
        :param value: value of datapoint instance.
        :param tooltip: tooltip of datapoint instance.
        :param measured_at: measured_at of datapoint instance.
        :return: datapoint models.
        """
        query = select(Datapoint)
        if id:
            query = query.where(Datapoint.id == id)
        if benchmark_run_id:
            query = query.where(Datapoint.benchmark_run_id == benchmark_run_id)
        if metric_name:
            query = query.where(Datapoint.metric_name == metric_name)
        if value:
            query = query.where(Datapoint.value == value)
        if tooltip:
            query = query.where(Datapoint.tooltip == tooltip)
        if measured_at:
            query = query.where(Datapoint.measured_at == measured_at)
        rows = self.session.execute(query)
        return list(rows.scalars().fetchall())

    def update_datapoint(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        benchmark_run_id: Optional[int] = None,
        metric_name: Optional[str] = None,
        value: Optional[float] = None,
        tooltip: Optional[str] = None,
        measured_at: Optional[datetime.datetime] = None,
    ) -> None:
        """
        Update specific datapoint model.

        :param id: id of datapoint instance.
        :param benchmark_run_id: benchmark_run_id of datapoint instance.
        :param metric_name: metric_name of datapoint instance.
        :param value: value of datapoint instance.
        :param tooltip: tooltip of datapoint instance.
        :param measured_at: measured_at of datapoint instance.
        """
        query = select(Datapoint)
        query = query.where(Datapoint.id == id)
        raw_datapoint = self.session.execute(query)
        datapoint = raw_datapoint.scalars().first()
        if datapoint is not None:
            if benchmark_run_id:
                setattr(datapoint, "benchmark_run_id", benchmark_run_id)
            if metric_name:
                setattr(datapoint, "metric_name", metric_name)
            if value:
                setattr(datapoint, "value", value)
            if tooltip:
                setattr(datapoint, "tooltip", tooltip)
            if measured_at:
                setattr(datapoint, "measured_at", measured_at)
