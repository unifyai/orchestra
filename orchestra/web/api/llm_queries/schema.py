import time
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    """
    Request model for chat completion based language model.

    Attributes:
        model (str): The model identifier.
        messages (List[Dict[str]]): List of messages for completion.
        temperature (float): The temperature parameter for generating completions.
        stream (bool): Whether to stream the response.
    """

    # This allows extra arguments through.
    model_config = ConfigDict(extra="allow")

    model: str = Field(json_schema_extra={"example": "gpt-4o-mini@openai"})
    messages: List[Dict[str, Any]] = Field(
        json_schema_extra={
            "example": [
                {
                    "role": "user",
                    "content": "Tell me a joke",
                },
            ],
        },
    )

    # openai args
    temperature: float = Field(0.9, json_schema_extra={"example": 0.9})
    stream: bool = Field(False, json_schema_extra={"example": False})
    max_tokens: int = Field(None, json_schema_extra={"example": 1024})
    frequency_penalty: float = None
    logit_bias: Dict[str, float] = None
    logprobs: bool = None
    top_logprobs: int = None
    n: int = None
    presence_penalty: float = None
    response_format: Dict[str, str] = None
    seed: int = None
    stop: Union[str, List[str]] = None
    top_p: float = None
    tools: List[Any] = None
    tool_choice: Any = None
    user: str = None

    # args that are for orchestra use only
    signature: str = None
    use_custom_keys: bool = False


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
    """

    model: str
    created: int = None
    id: str = None
    object: str = "chat.completion"
    usage: Dict[str, Any]
    choices: List[Dict[str, Any]]

    def __init__(self, **data):
        super().__init__(**data)
        if self.created is None:
            self.created = int(time.time())
        if self.id is None:
            self.id = "msg-id"


class RouterScoresResponse(BaseModel):
    scores: Dict[str, float]


class QueryMetricsRequest(BaseModel):
    secondary_user_id: Optional[str] = ""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    models: Optional[str] = None
    providers: Optional[str] = None
    interval: Optional[str] = 300
