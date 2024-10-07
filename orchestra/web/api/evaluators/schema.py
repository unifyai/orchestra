from typing import Dict, List, Optional, Union

import unify
from pydantic import BaseModel, Field


class EvaluatorConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing and triggering "
        "the evaluation.",
        json_schema_extra={"example": "eval1"},
    )
    description: Optional[str] = Field(
        default=None,
        description="Description for the purpose of this evaluator. "
        "In the case of Evaluators defined in the Python client, this is "
        "the docstring by default, if it exists.",
        json_schema_extra={"example": "The clarity of the generated summary"},
    )
    judge_prompt: Optional[Union[str, unify.Prompt]] = Field(
        default=None,
        description="An optional custom system prompt to provide specific instructions "
        "to the judge on how to score the answers.",
    )
    prompt_parser: Optional[Dict[str, List[Union[str, int]]]] = Field(
        default={"user_message": ["messages", -1, "content"]},
        description="Dict with str  keys and indexing logic values. Default value of: `{'user_message': ['messages', -1, 'content']}` "
        "This is used by the system prompt to replace each key {some_key} with prompt.dict()\<indexing_logic\> "
        "The default value will replace all occurances of {user_message} with prompt.dict()['messages'][-1]['content'] in the judge prompt.",
    )
    response_parser: Optional[Dict[str, List[Union[str, int]]]] = Field(
        default={"assistant_message": ["message", "content"]},
        description="Dict with str  keys and indexing logic values. Default value of: `{'assistant_response': ['message', 'content']}` "
        "This is used by the system prompt to replace each key {some_key} with response.dict()\<indexing_logic\> "
        "The default value will replace all occurances of {assisntant_message} with response.dict()['message']['content'] in the judge prompt.",
    )
    extra_parser: Optional[Dict[str, List[Union[str, int]]]] = Field(
        default=None,
        description="Dict with str  keys and indexing logic values. Default value of: `None` "
        "This is used by the system prompt to replace each key {some_key} with datum.dict()\<indexing_logic\> "
        "which can be used to index into extra fields stored within each item in the dataset.",
    )
    class_config: Optional[List[Dict[str, Union[str, float]]]] = Field(
        default=None,
        description=(
            """If set, describes the list of classifications that the LLM judge uses to
            score each prompt. For example:
```
[{"label": "Excellent", "score": 1.0},
{"label": "Good", "score": 0.5},
{"label": "Bad", "score": 0.0}]
```
"""
        ),
    )
    judge_models: Union[str, List[str]] = Field(
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
