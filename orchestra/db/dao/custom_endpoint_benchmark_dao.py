from datetime import datetime

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomEndpointBenchmark


class CustomEndpointBenchmarkDAO:
    """Class for accessing custom endpoint benchmark table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def upload_benchmark(
        self,
        endpoint_id: str,
        metric_name: str,
        value: float,
        measured_at: datetime,
    ):
        self.session.add(
            CustomEndpointBenchmark(
                custom_endpoint_id=endpoint_id,
                metric_name=metric_name,
                value=value,
                measured_at=measured_at,
            ),
        )

    def benchmarks_between(
        self,
        endpoint_id,
        metric_name,
        start_time,
        end_time,
    ):
        query = (
            select(CustomEndpointBenchmark)
            .where(CustomEndpointBenchmark.custom_endpoint_id == endpoint_id)
            .where(CustomEndpointBenchmark.metric_name == metric_name)
            .where()
            .filter(
                CustomEndpointBenchmark.measured_at >= start_time,
                CustomEndpointBenchmark.measured_at <= end_time,
            )
        )
        data = self.session.execute(query)
        return list(data.scalars().fetchall())
