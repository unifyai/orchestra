from typing import Any

from pydantic import BaseModel, Field


class DefaultPromptConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing  "
        "the default prompt.",
        json_schema_extra={"example": "eval1"},
    )
    prompt: dict[str, Any] = Field(
        default=False,
        description="Prompt fields that will override any field in the "
        "prompt to be evaluated when triggering an evaluation.",
    )
