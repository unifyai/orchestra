from typing import Dict, List, Mapping, Optional, Union

from openai._types import Body, Query
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, Field


class Prompt(BaseModel):
    messages: Optional[List[ChatCompletionMessageParam]] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[Dict[str, int]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    max_tokens: Optional[int] = 1024
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    response_format: Optional[ResponseFormat] = None
    seed: Optional[int] = None
    stop: Union[Optional[str], List[str]] = None
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    tools: Optional[List[ChatCompletionToolParam]] = None
    tool_choice: Optional[ChatCompletionToolChoiceOptionParam] = None
    parallel_tool_calls: Optional[bool] = None
    # extra_headers: Optional[Headers] = None  # ToDo: fix Omit error
    extra_headers: Optional[Mapping[str, str]] = None
    extra_query: Optional[Query] = None
    extra_body: Optional[Body] = None


class EvaluatorConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing and triggering "
        "the evaluation.",
        json_schema_extra={"example": "eval1"},
    )
    judge_prompt: Optional[Union[str, Prompt]] = Field(
        default=None,
        description="An optional custom system prompt to provide specific instructions "
        "to the judge on how to score the answers.",
    )
    class_config: Union[list, None] = Field(
        default=None,
        description=(
            """If set, describes the list of classifications that the LLM judge uses to
            score each prompt. For example:
```
[{"label": "Excellent", "score": 1.0, "description": "A perfect answer with no factual
mistakes"},
{"label": "Good", "score": 0.5, "description": "An average answer"},
{"label": "Bad", "score": 0.0, "description": "An incorrect answer, containing a
significant factual mistake"}]
```
"""
        ),
    )
    judge_models: Union[str, list[str]] = Field(
        default="claude-3.5-sonnet@aws-bedrock",
        description="Specifies the LLM(s) to be used as the judge. This can be a "
        "string containing a single model name or a list of model names.",
        json_schema_extra={"example": "claude-3.5-sonnet@aws-bedrock"},
    )
    client_side: bool = Field(
        default=False,
        description="Indicates whether evaluations are performed on the client-side. "
        "If `True`, the LLM judge is bypassed, and results are uploaded "
        "via the `trigger` endpoint.",
    )
