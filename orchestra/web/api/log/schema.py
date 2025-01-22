from typing import Any, Dict, List, Tuple, Union

from pydantic import BaseModel, Field

from orchestra.web.api.context.schema import ContextCreateRequest


class CreateLogConfig(BaseModel):
    project: str = Field(
        description="Name of the project the stored entries will be associated to.",
        json_schema_extra={
            "example": "eval-project",
        },
    )
    context: ContextCreateRequest | None = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1').",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    params: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Can be either a single dictionary or a list of dictionaries "
        "for batch processing. When using lists for both params and entries, their lengths must match. "
        "Parameters will be automatically versioned based on their values. "
        "Values must be JSON serializable. If a `explicit_types` dictionary is present, its values "
        "will override the inferred types of the entries.",
        json_schema_extra={
            "examples": [
                {
                    "system-prompt": "...",
                    "function_definition": "...",
                    "explicit_types": {"system-prompt": "str"},
                },
                [
                    {"system-prompt": "prompt1"},
                    {"system-prompt": "prompt2"},
                ],
            ],
        },
    )
    entries: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Can be either a single dictionary or a list of dictionaries "
        "for batch processing. When using lists for both params and entries, their lengths must match. "
        "Values must be JSON serializable. If a `explicit_types` dictionary is present, "
        "its values will override the inferred types of the entries.",
        json_schema_extra={
            "examples": [
                {
                    "input": "...",
                    "score-test-1": "...",
                    "explicit_types": {"input": "Image"},
                },
                [
                    {"input": "test1", "score": 0.8},
                    {"input": "test2", "score": 0.9},
                ],
            ],
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
    context: ContextCreateRequest | None = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1').",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
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
    ids_and_fields: List[Tuple[Union[int, List[int]], Union[None, str, List[str]]]] = (
        Field(
            description="List of tuples of log ID(s) and field(s) to delete, "
            "either as an individual item or a list of items.",
            example=[
                (123, "score"),
                ([456, 457], ["score", "response"]),
                ([458, 459, 460], "response"),
            ],
            min_items=1,
        )
    )


class SetFieldTypingRequest(BaseModel):
    types: Dict[str, bool] = Field(
        ...,
        description="Dict of field names and booleans as to whether the field should be typed.",
    )
