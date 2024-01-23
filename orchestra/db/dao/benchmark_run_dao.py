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
        input_seq_len: int,
        output_seq_len: int,
    ) -> None:
        """
        Add single benchmark_run to session.

        :param endpoint_id: endpoint_id of a benchmark_run.
        :param regime: regime of a benchmark_run.
        :param region: region of a benchmark_run.
        :param input_seq_len: input_seq_len of a benchmark_run.
        :param output_seq_len: output_seq_len of a benchmark_run.
        """
        self.session.add(
            BenchmarkRun(
                endpoint_id=endpoint_id,
                regime=regime,
                region=region,
                input_seq_len=input_seq_len,
                output_seq_len=output_seq_len,
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
        input_seq_len: Optional[int] = None,
        output_seq_len: Optional[int] = None,
    ) -> List[BenchmarkRun]:
        """
        Filter benchmark_run models by given parameters.

        :param id: id of a benchmark_run.
        :param endpoint_id: endpoint_id of a benchmark_run.
        :param regime: regime of a benchmark_run.
        :param region: region of a benchmark_run.
        :param input_seq_len: input_seq_len of a benchmark_run.
        :param output_seq_len: output_seq_len of a benchmark_run.
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
        if input_seq_len:
            query = query.where(BenchmarkRun.input_seq_len == input_seq_len)
        if output_seq_len:
            query = query.where(BenchmarkRun.output_seq_len == output_seq_len)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
