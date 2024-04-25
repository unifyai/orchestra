from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class EvalBatchRequest(BaseModel):
    """
    Request model for batch evaluation of prompts.
    """

    # TODO: This removes all other params if not specified
    model: str
    messages: List[Dict[str, str]]
    temperature: float = 0.9
    stream: bool = False
    max_tokens: Optional[int] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    response_format: Optional[str] = None
    seed: Optional[int] = None
    stop: Optional[str] = None
    top_p: Optional[float] = None
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None
    user: Optional[str] = None


class EvalBatchResponse(BaseModel):
    """
    Response model for batch evaluation of prompts.
    """

    info: str
