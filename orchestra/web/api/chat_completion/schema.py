from pydantic import BaseModel, ConfigDict


class ChatCompletionRequest(BaseModel):
    """
    chat completion based language model requests
    """
    model: str
    messages: List[dict]
    temperature: float

class ChatCompletionResponse(BaseModel):
    """
    chat completion based language model responses
    """
    model: str
    created: float
    id: str
    object: str =  "chat.completion"
    usage: dict
    choices: List[dict]