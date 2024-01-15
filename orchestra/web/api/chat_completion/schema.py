from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ChatCompletionRequest(BaseModel):
    """
    Request model for chat completion based language model.

    Attributes:
        model (str): The model identifier.
        messages (List[Dict[str]]): List of messages for completion.
        temperature (float): The temperature parameter for generating completions.
        stream (bool): Whether to stream the response.
    """

    model: str
    messages: List[Dict[str, str]]
    temperature: float = 0.9
    stream: bool = False


class ChatCompletionResponse(BaseModel):
    """
    Response model for chat completion based language model.

    Attributes:
        model (str): The model identifier.
        created (int): Timestamp indicating when the response was created.
        id (str): Identifier for the completion response.
        object (str): The type of object, defaults to "chat.completion".
        usage (dict): Usage statistics or additional information.
        choices (List[Dict]): List of completion choices.
        _response_ms (float): Response time in milliseconds.
    """

    model: str
    created: int
    id: Optional[str] = None
    object: str = "chat.completion"
    usage: Dict[str, Any]
    choices: List[Dict[str, Any]]
    _response_ms: float

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._response_ms = data["_response_ms"]

    def __setattr__(self, name, value):
        self.__dict__[name] = value
