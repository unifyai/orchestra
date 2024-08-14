from typing import Union

from pydantic import BaseModel, Field


class EvalConfig(BaseModel):
    eval_name: str = Field(..., example="eval1")
    system_prompt: Union[str, None] = None
    class_config: Union[dict, None] = None
    judge_models: Union[str, list[str], None] = Field(
        "claude-3.5-sonnet@aws-bedrock",
        example="claude-3.5-sonnet@aws-bedrock",
    )
    client_side: bool = False
