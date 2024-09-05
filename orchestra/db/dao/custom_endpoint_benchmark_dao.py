import os
from datetime import datetime

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CustomEndpointBenchmark
from orchestra.web.api.utils.on_prem import OnPremModel


class CustomEndpointBenchmarkDAO:
    """Class for accessing custom endpoint benchmark table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session
        self.on_prem = os.environ.get("ON_PREM")
        if self.on_prem:
            self.on_prem_model = OnPremModel(
                model_class=CustomEndpointBenchmark,
                table_name="custom_endpoint_benchmark",
            )

    def upload_benchmark(
        self,
        endpoint_id: str,
        metric_name: str,
        value: float,
        measured_at: datetime,
    ):
        data = {
            "custom_endpoint_id": endpoint_id,
            "metric_name": metric_name,
            "value": value,
            "measured_at": str(measured_at),
        }
        if self.on_prem:
            self.on_prem_model.create(**data)
        else:
            self.session.add(CustomEndpointBenchmark(**data))

    def benchmarks_between(
        self,
        endpoint_id,
        metric_name,
        start_time,
        end_time,
    ):
        if self.on_prem:
            return self.on_prem_model.read(
                filters={
                    "custom_endpoint_benchmark": {
                        "custom_endpoint_id": endpoint_id,
                        "metric_name": metric_name,
                        "measured_at": lambda measured_at: (
                            datetime.fromisoformat(measured_at)
                            >= datetime.fromisoformat(start_time)
                            and datetime.fromisoformat(measured_at)
                            <= datetime.fromisoformat(end_time)
                        ),
                    },
                },
            )
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
