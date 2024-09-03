import datetime
from typing import Optional

from pydantic import BaseModel


class QueryModelRequest(BaseModel):
    """
    Request model for creating new query model.

    Attributes:
        user_id (str): The id of the user.
        at (datetime): The time of the query.
        endpoint_id (int): The id of the endpoint.
        credits (float): The credits of the query.
    """

    user_id: str
    model_provider_str: str
    endpoint_id: Optional[int]
    custom_endpoint_id: Optional[int]
    local_endpoint_id: Optional[int]
    credits: float
    query_body: Optional[str]
    response_body: Optional[str]
    signature: Optional[str]
    used_router: Optional[bool]
    router: Optional[str]
    tags: Optional[list[str]]


class QueryModelResponse(BaseModel):
    """
    Response model for query models.

    Attributes:
        id (int): The id of the query.
        user_id (str): The id of the user.
        at (datetime): The time of the query.
        endpoint_id (int): The id of the endpoint.
        credits (float): The credits of the query.
    """

    id: int
    user_id: str
    at: datetime.datetime
    model_provider_str: str
    endpoint_id: int
    credits: float
    query_body: Optional[str]
    response_body: Optional[str]
    signature: Optional[str]
    used_router: Optional[bool]
    router: Optional[str]
