from pydantic import BaseModel
from typing import Dict


class BenchmarksRequest(BaseModel):
    """
    Request model for benchmark queries.
    """

    model: str
    provider: str
    region: str
    seq_len: str


class BenchmarksResponse(BaseModel):
    """
    Response model for benchmark queries.
    """

    model: str
    provider: str
    metrics: Dict[str, float]
