import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import BenchmarkRun


class BenchmarkRunDAO:
    """Class for accessing benchmark_run table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_benchmark_run(  # noqa: WPS211
        self,
        endpoint_id: int,
        regime: str,
        region: str,
        seq_len: str,
        measured_at: datetime.datetime,
    ) -> None:
        """
        Add single benchmark_run to session.

        :param endpoint_id: endpoint_id of a benchmark_run.
        :param regime: regime of a benchmark_run.
        :param region: region of a benchmark_run.
        :param seq_len: seq_len of a benchmark_run.
        :param measured_at: measured_at of a benchmark_run.
        """
        self.session.add(
            BenchmarkRun(
                endpoint_id=endpoint_id,
                regime=regime,
                region=region,
                seq_len=seq_len,
                measured_at=measured_at,
            ),
        )

    async def get_all_benchmark_runs(
        self,
        limit: int,
        offset: int,
    ) -> List[BenchmarkRun]:
        """
        Get all benchmark_run models with limit/offset pagination.

        :param limit: limit of benchmark_runs.
        :param offset: offset of benchmark_runs.
        :return: stream of benchmark_runs.
        """
        raw_benchmark_runs = await self.session.execute(
            select(BenchmarkRun).limit(limit).offset(offset),
        )

        return list(raw_benchmark_runs.scalars().fetchall())

    async def filter(  # noqa: WPS211, C901
        self,
        id: Optional[int] = None,  # noqa: WPS125
        endpoint_id: Optional[int] = None,
        regime: Optional[str] = None,
        region: Optional[str] = None,
        seq_len: Optional[str] = None,
        measured_at: Optional[datetime.datetime] = None,
    ) -> List[BenchmarkRun]:
        """
        Filter benchmark_run models by given parameters.

        :param id: id of a benchmark_run.
        :param endpoint_id: endpoint_id of a benchmark_run.
        :param regime: regime of a benchmark_run.
        :param region: region of a benchmark_run.
        :param seq_len: seq_len of a benchmark_run.
        :param measured_at: measured_at of a benchmark_run.
        :return: benchmark_runs.
        """
        query = select(BenchmarkRun)
        if id:
            query = query.where(BenchmarkRun.id == id)
        if endpoint_id:
            query = query.where(BenchmarkRun.endpoint_id == endpoint_id)
        if regime:
            query = query.where(BenchmarkRun.regime == regime)
        if region:
            query = query.where(BenchmarkRun.region == region)
        if seq_len:
            query = query.where(BenchmarkRun.seq_len == seq_len)
        if measured_at:
            query = query.where(BenchmarkRun.measured_at == measured_at)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())

    async def update_benchmark_run(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        endpoint_id: Optional[int] = None,
        regime: Optional[str] = None,
        region: Optional[str] = None,
        seq_len: Optional[str] = None,
        measured_at: Optional[datetime.datetime] = None,
    ) -> None:
        """
        Update specific benchmark_run model.

        :param id: id of benchmark_run instance.
        :param endpoint_id: endpoint_id of benchmark_run instance.
        :param regime: regime of benchmark_run instance.
        :param region: region of benchmark_run instance.
        :param seq_len: seq_len of benchmark_run instance.
        :param measured_at: measured_at of benchmark_run instance.
        """
        query = select(BenchmarkRun)
        query = query.where(BenchmarkRun.id == id)
        raw_benchmark_run = await self.session.execute(query)
        benchmark_run = raw_benchmark_run.scalars().first()
        if benchmark_run is not None:
            if endpoint_id:
                setattr(benchmark_run, "endpoint_id", endpoint_id)  # noqa: B010
            if regime:
                setattr(benchmark_run, "regime", regime)  # noqa: B010
            if region:
                setattr(benchmark_run, "region", region)  # noqa: B010
            if seq_len:
                setattr(benchmark_run, "seq_len", seq_len)  # noqa: B010
            if measured_at:
                setattr(benchmark_run, "measured_at", measured_at)  # noqa: B010
