from typing import Union

from pydantic import BaseModel, Field


class EvalConfig(BaseModel):
    eval_name: str = Field(
        description="A unique, user-defined name used when referencing and triggering the eval.",
        json_schema_extra={"example": "eval1"},
    )
    system_prompt: Union[str, None] = Field(
        default=None,
        description="An optional custom system prompt to provide specific instructions to the judge on how to score the answers.",
    )
    class_config: Union[list, None] = Field(
        default=None,
        description=(
            """If set, describes the list of classifications that the LLM judge uses to score each prompt. For example:
```
[{"label": "Excellent", "score": 1.0, "description": "A perfect answer with no factual mistakes"},
{"label": "Good", "score": 0.5, "description": "An average answer"},
{"label": "Bad", "score": 0.0, "description": "An incorrect answer, containing a significant factual mistake"}]
```
"""
        ),
    )
    judge_models: Union[str, list[str]] = Field(
        default="claude-3.5-sonnet@aws-bedrock",
        description="Specifies the LLM(s) to be used as the judge. This can be a string containining a single model name or a list of model names.",
        json_schema_extra={"example": "claude-3.5-sonnet@aws-bedrock"},
    )
    client_side: bool = Field(
        default=False,
        description="Indicates whether evaluations are performed on the client-side. If `True`, the LLM judge is bypassed, and results are uploaded via the `trigger` endpoint.",
    )
