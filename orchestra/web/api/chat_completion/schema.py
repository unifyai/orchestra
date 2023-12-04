from typing import List
from pydantic import BaseModel


class ChatCompletionRequest(BaseModel):
    """
    Chat completion based language model requests.
    """
    model: str
    messages: List[dict]
    temperature: float


class ChatCompletionResponse(BaseModel):
    """
    Chat completion based language model responses.
    """
    model: str
    created: float
    id: str
    object: str = "chat.completion"
    usage: dict
    choices: List[dict]
