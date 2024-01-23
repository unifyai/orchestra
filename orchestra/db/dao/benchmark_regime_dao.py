from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import BenchmarkRegime


class BenchmarkRegimeDAO:
    """Class for accessing benchmark_regime table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_benchmark_regime(self, name: str) -> None:
        """
        Add single benchmark_regime to session.

        :param name: name of a benchmark_regime.
        """
        self.session.add(BenchmarkRegime(name=name))

    async def get_all_benchmark_regimes(
        self,
        limit: int,
        offset: int,
    ) -> List[BenchmarkRegime]:
        """
        Get all benchmark_regime models with limit/offset pagination.

        :param limit: limit of benchmark_regimes.
        :param offset: offset of benchmark_regimes.
        :return: stream of benchmark_regimes.
        """
        raw_benchmark_regimes = await self.session.execute(
            select(BenchmarkRegime).limit(limit).offset(offset),
        )

        return list(raw_benchmark_regimes.scalars().fetchall())

    async def filter(
        self,
        name: Optional[str] = None,
    ) -> List[BenchmarkRegime]:
        """
        Filter benchmark_regime models.

        :param name: name of a benchmark_regime.
        :return: stream of benchmark_regimes.
        """
        query = select(BenchmarkRegime)

        if name is not None:
            query = query.where(BenchmarkRegime.name == name)

        raw_benchmark_regimes = await self.session.execute(query)
        return list(raw_benchmark_regimes.scalars().fetchall())
