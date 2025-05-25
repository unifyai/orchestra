from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import BenchmarkRegion


class BenchmarkRegionDAO:
    """Class for accessing benchmark_region table."""

    def __init__(self, session: Session):
        self.session = session

    def create_benchmark_region(self, name: str) -> None:
        """
        Add single benchmark_region to session.

        :param name: name of a benchmark_region.
        """
        self.session.add(BenchmarkRegion(name=name))

    def get_all_benchmark_regions(
        self,
        limit: int,
        offset: int,
    ) -> List[BenchmarkRegion]:
        """
        Get all benchmark_region models with limit/offset pagination.

        :param limit: limit of benchmark_regions.
        :param offset: offset of benchmark_regions.
        :return: stream of benchmark_regions.
        """
        raw_benchmark_regions = self.session.execute(
            select(BenchmarkRegion).limit(limit).offset(offset),
        )

        return list(raw_benchmark_regions.scalars().fetchall())

    def filter(
        self,
        name: Optional[str] = None,
    ) -> List[BenchmarkRegion]:
        """
        Filter benchmark_region models.

        :param name: name of a benchmark_region.
        :return: stream of benchmark_regions.
        """
        query = select(BenchmarkRegion)

        if name is not None:
            query = query.where(BenchmarkRegion.name == name)

        raw_benchmark_regions = self.session.execute(query)

        return list(raw_benchmark_regions.scalars().fetchall())
