from typing import Any, Dict

from pydantic import BaseModel, Field


class CreateLogConfig(BaseModel):
    project: str = Field(
        description="Name of the project the stored entries will be associated to.",
        json_schema_extra="eval-project",
    )
    entries: Dict[str, Any] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Keys can have an optional "
        "version defined after a forward slash. E.g. `system_msg/v1`. "
        "If defined, these versions will be used when grouping results on "
        "a per-key basis. Values must be JSON serializable.",
        json_schema_extra={
            "example": {"input": "...", "score-test-1": "..."},
        },
    )


class UpdateLogConfig(BaseModel):
    entries: Dict[str, Any] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Keys can have an optional "
        "version defined after a forward slash. E.g. `system_msg/v1`. "
        "If defined, these versions will be used when grouping results on "
        "a per-key basis. Values must be JSON serializable.",
        json_schema_extra={
            "example": {"input": "...", "score-test-1": "..."},
        },
    )
