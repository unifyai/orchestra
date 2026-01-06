"""Async version of custom_endpoint_benchmark_dao for use with AsyncSession."""

from datetime import datetime
from typing import List, Optional, Union

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import CustomEndpointBenchmark


class AsyncCustomEndpointBenchmarkDAO:
    """Class for accessing custom endpoint benchmark table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upload_benchmark(
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

    async def benchmarks_between(
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
            .filter(
                CustomEndpointBenchmark.measured_at >= start_time,
                CustomEndpointBenchmark.measured_at <= end_time,
            )
        )
        data = await self.session.execute(query)
        return list(data.scalars().fetchall())

    async def delete(
        self,
        endpoint_id: Union[int, List[int]],
        timestamps: Optional[Union[datetime, List[datetime]]] = None,
    ):
        endpoint_id = endpoint_id if isinstance(endpoint_id, list) else [endpoint_id]
        query = delete(CustomEndpointBenchmark).where(
            or_(
                *[CustomEndpointBenchmark.custom_endpoint_id == i for i in endpoint_id],
            ),
        )
        if timestamps is not None:
            timestamps = timestamps if isinstance(timestamps, list) else [timestamps]
            query = query.where(
                or_(*[CustomEndpointBenchmark.measured_at == t for t in timestamps]),
            )
        await self.session.execute(query)
