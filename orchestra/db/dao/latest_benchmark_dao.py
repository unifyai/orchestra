from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Endpoint,
    LatestBenchmark,
    Model,
    Provider,
)


class LatestBenchmarkDAO:
    """Class for accessing latest_benchmark table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def get_latest_benchmarks(self, endpoint_id, regime, region, seq_len):
        """
        Gets the latest benchmark run for each provider for a given model.
        """

        query = (
            select(LatestBenchmark)
            .where(LatestBenchmark.endpoint_id == endpoint_id)
            .where(LatestBenchmark.regime == regime)
            .where(LatestBenchmark.region == region)
            .where(LatestBenchmark.seq_len == seq_len)
        )
        data = self.session.execute(query)
        return list(data.scalars().fetchall())

    def get_benchmark_with_endpoints(self):
        """
        Gets detailed benchmark data with endpoint_str, ttft, itl, input_cost, and output_cost.
        """
        query = (
            select(
                func.concat(Model.mdl_code, "@", Provider.name).label("endpoint_str"),
                LatestBenchmark.ttft,
                LatestBenchmark.itl,
                LatestBenchmark.input_cost,
                LatestBenchmark.output_cost,
            )
            .join(Endpoint, LatestBenchmark.endpoint_id == Endpoint.id)
            .join(Provider, Endpoint.provider_id == Provider.id)
            .join(Model, Endpoint.mdl_id == Model.id)
            .where(LatestBenchmark.region == "Belgium")
            .where(LatestBenchmark.regime == "concurrent-1")
            .where(LatestBenchmark.seq_len == "short")
        )
        data = self.session.execute(query)
        return data.fetchall()
