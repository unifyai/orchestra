import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    LatestBenchmark,
)


class LatestBenchmarkDAO:
    """Class for accessing latest_benchmark table."""

    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def get_latest_benchmarks(self, endpoint_id):
        """
        Gets the latest benchmark run for each provider for a given model.
        """

        query = select(LatestBenchmark).where(
            LatestBenchmark.endpoint_id == endpoint_id
        )
        data = self.session.execute(query)
        return list(data.scalars().fetchall())
