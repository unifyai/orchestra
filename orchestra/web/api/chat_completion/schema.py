from typing import Any, Dict, List, Optional, Union

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

    # TODO: This removes all other params if not specified
    model: str
    messages: List[Dict[str, Any]]
    temperature: float = 0.9
    stream: bool = False
    max_tokens: Optional[int] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    response_format: Optional[Dict[str, str]] = None
    seed: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    top_p: Optional[float] = None
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None
    user: Optional[str] = None
    signature: Optional[str] = None


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
    created: int
    id: Optional[str] = None
    object: str = "chat.completion"
    usage: Dict[str, Any]
    choices: List[Dict[str, Any]]


class RouterScoresResponse(BaseModel):
    scores: Dict[str, float]


class QueryMetricsRequest(BaseModel):
    secondary_user_id: Optional[str] = ""
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    models: Optional[str] = None
    providers: Optional[str] = None
    interval: Optional[str] = 300
