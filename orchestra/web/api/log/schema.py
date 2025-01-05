from typing import Any, Dict, List, Tuple, Union

from pydantic import BaseModel, Field


class CreateLogConfig(BaseModel):
    project: str = Field(
        description="Name of the project the stored entries will be associated to.",
        json_schema_extra={
            "example": "eval-project",
        },
    )
    params: Dict[str, Any] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Parameters will be automatically "
        "versioned based on their values. Values must be JSON serializable. "
        "If a `explicit_types` dictionary is present, its values "
        "will override the inferred types of the entries.",
        json_schema_extra={
            "example": {
                "system-prompt": "...",
                "function_definition": "...",
                "explicit_types": {"system-prompt": "str"},
            },
        },
    )
    entries: Dict[str, Any] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Values must be JSON serializable. "
        "If a `explicit_types` dictionary is present, its values "
        "will override the inferred types of the entries.",
        json_schema_extra={
            "example": {
                "input": "...",
                "score-test-1": "...",
                "explicit_types": {"input": "Image"},
            },
        },
    )
    strongly_typed: Union[bool, List[str]] = Field(
        default=True,
        description="Enforce strong typing for fields.",
    )


class CreateDerivedEntriesConfig(BaseModel):
    project: str = Field(
        description="Name of the project the stored entries will be associated to.",
        json_schema_extra={
            "example": "eval-project",
        },
    )
    key: str = Field(
        description="The name of the entry.",
        example="score_diff",
    )
    equation: str = Field(
        description="The equation for computing the value of each derived entry.",
        example="{log0:score} - {log1:score}",
    )
    referenced_logs: Dict[str, Union[List[int], Dict[str, Any]]] = Field(
        description="The logs to use for each newly created derived entry, either as "
        "a list of log ids or as a set of arguments for the get_logs "
        "endpoint.",
        example={"log0": [0, 1, 2], "log1": {"filter_expr": "score > 0.5"}},
    )


class UpdateLogRequest(BaseModel):
    ids: list[int] = Field(
        description="List of log IDs to update with new or overriding entries.",
        example=[123, 456, 789],
        min_items=1,
    )
    params: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary or list of dictionaries of key-value parameter pairs to add or update in the logs.",
        json_schema_extra={
            "example": {
                "system-prompt": "...",
                "function_definition": "...",
                "explicit_types": {"system-prompt": "str"},
            },
        },
    )
    entries: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary or list of dictionaries of key-value entry pairs to add or update in the logs.",
        json_schema_extra={
            "example": {
                "input": "...",
                "score-test-1": "...",
                "explicit_types": {"input": "Image"},
            },
        },
    )
    overwrite: bool = Field(
        default=False,
        description="Whether to overwrite existing logs",
        example=False,
    )
    strongly_typed: Union[bool, List[str]] = Field(
        default=True,
        description="Enforce strong typing for fields.",
    )


class DeleteLogsRequest(BaseModel):
    ids: list[int] = Field(
        description="List of log IDs to delete.",
        example=[123, 456, 789],
        min_items=1,
    )


class DeleteLogEntryRequest(BaseModel):
    fields: List[Tuple[Union[int, List[int]], Union[str, List[str]]]] = Field(
        description="List of lists of log ID(s) and field(s) to delete, "
        "either as an individual item or a list of items.",
        example=[
            (123, "score"),
            ([456, 457], ["score", "response"]),
            ([458, 459, 460], "response"),
        ],
        min_items=1,
    )


class SetFieldTypingRequest(BaseModel):
    types: Dict[str, bool] = Field(
        ...,
        description="Dict of field names and booleans as to whether the field should be typed.",
    )
