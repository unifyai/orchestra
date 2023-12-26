from typing import Any, Dict

from pydantic import BaseModel


class InferenceRequest(BaseModel):
    """
    Request model for any model in the hub.

    Attributes:
        model (str): The model identifier.
        provider (str): The provider identifier.
        arguments (Dict[str, Any]]): Model-specific arguments.
    """

    model: str
    provider: str
    arguments: Dict[str, Any]


class InferenceResponse(BaseModel):
    """
    Response model for any model in the hub.

    Attributes:
        response (Dict[str, Any]): Model-specific response.
    """

    response: Dict[str, Any]
