import datetime

from pydantic import BaseModel


class EndpointModelRequest(BaseModel):
    """
    Request model for creating new endpoint model.

    Attributes:
        mdl_id (int): The id of the model.
        provider_id (int): The id of the provider.
    """

    mdl_id: int
    provider_id: int


class EndpointModelResponse(BaseModel):
    """
    Response model for endpoint models.

    Attributes:
        id (int): The id of the endpoint.
        mdl_id (int): The id of the model.
        provider_id (int): The id of the provider.
        created_at (datetime): The time the endpoint was created.
    """

    id: int
    mdl_id: int
    provider_id: int
    created_at: datetime.datetime
