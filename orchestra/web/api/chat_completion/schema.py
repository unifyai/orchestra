from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ChatCompletionRequest(BaseModel):
    """
    Request model for chat completion based language model.

    Attributes:
        model (str): The model identifier.
        messages (List[Dict]): List of messages for completion.
        temperature (float): The temperature parameter for generating completions.
    """

    model: str
    messages: str
    temperature: float


class ChatCompletionResponse(BaseModel):
    """
    Response model for chat completion based language model.

    Attributes:
        model (str): The model identifier.
        created (float): Timestamp indicating when the response was created.
        id (str): Identifier for the completion response.
        object (str): The type of object, defaults to "chat.completion".
        usage (dict): Usage statistics or additional information.
        choices (List[Dict]): List of completion choices.
    """

    model: str
    created: float
    id: Optional[str] = None
    object: str = "chat.completion"
    usage: Dict[str, Any]
    choices: List[Dict[str, Any]]
