from typing import Union
from pydantic import BaseModel


class EvalConfig(BaseModel):
    eval_name: str
    system_prompt: Union[str, None] = None
    class_config: Union[dict, None] = None
    judge_models: Union[str, list[str], None] = "claude-3.5-sonnet@aws-bedrock"
    client_side: bool = False
