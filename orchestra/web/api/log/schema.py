from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator

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
        "will override the inferred types of the entries. The explicit_types dictionary can also specify if a field is mutable via a 'mutable' boolean flag.",
        json_schema_extra={
            "examples": [
                {
                    "system-prompt": "...",
                    "function_definition": "...",
                    "explicit_types": {
                        "system-prompt": {"type": "str", "mutable": True},
                    },
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
        "its values will override the inferred types of the entries. The explicit_types dictionary can also specify if a field is mutable via a 'mutable' boolean flag.",
        json_schema_extra={
            "examples": [
                {
                    "input": "...",
                    "score-test-1": "...",
                    "explicit_types": {"input": {"type": "Image", "mutable": True}},
                },
                [
                    {"input": "test1", "score": 0.8},
                    {"input": "test2", "score": 0.9},
                ],
            ],
        },
    )


class CreateDerivedEntriesConfig(BaseModel):
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
                "explicit_types": {"system-prompt": {"type": "str", "mutable": True}},
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
                "explicit_types": {"input": {"type": "Image", "mutable": True}},
            },
        },
    )
    overwrite: bool = Field(
        default=False,
        description="Whether to overwrite existing logs",
        example=False,
    )


class DeleteLogsRequest(BaseModel):
    ids: list[int] = Field(
        description="List of log IDs to delete.",
        example=[123, 456, 789],
        min_items=1,
    )


class DeleteLogEntryRequest(BaseModel):
    project: str = Field(
        description="Name of the project the logs belong to.",
        example="eval-project",
    )
    context: ContextCreateRequest | None = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1').",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    ids_and_fields: List[
        Tuple[Union[int, List[int]], Union[None, str, List[str]]]
    ] = Field(
        description="List of tuples of log ID(s) and field(s) to delete, "
        "either as an individual item or a list of items.",
        example=[
            (123, "score"),
            ([456, 457], ["score", "response"]),
            ([458, 459, 460], "response"),
        ],
        min_items=1,
    )
    source_type: str = Field(
        default="all",
        description="Specifies which type of logs to delete. Can be 'base' for base logs only, "
        "'derived' for derived logs only, or 'all' to delete from both types.",
        json_schema_extra={
            "example": "all",
            "enum": ["base", "derived", "all"],
        },
    )


class UpdateDerivedEntriesConfig(BaseModel):
    project: str = Field(
        description="Name of the project these derived logs belong to.",
        example="eval-project",
    )
    target_derived_logs: Union[List[int], Dict[str, Any]] = Field(
        description="The derived logs to update, either as a list of derived_log IDs or as a set of arguments for the get_logs endpoint.",
        example={"log0": [0, 1, 2], "log1": {"filter_expr": "derived_score > 0.5"}},
    )
    key: Optional[str] = Field(
        default=None,
        description="New key name for the derived entries",
        example="temp_plus_20",
    )
    equation: Optional[str] = Field(
        default=None,
        description="New equation for computing derived values",
        example="{t:temperature} + 20",
    )
    referenced_logs: Optional[Dict[str, Union[List[int], Dict[str, Any]]]] = Field(
        default=None,
        description="Optional new referenced logs to use for computation. Can be specified either as "
        "a list of log IDs or as a set of arguments for the get_logs endpoint.",
        example={"t": [1, 2, 3], "other": {"filter_expr": "score > 0.5"}},
    )

    @model_validator(mode="before")
    def validate_params(cls, values):
        if not values.get("target_derived_logs"):
            raise ValueError(
                "target_derived_logs must be provided. Either as a list of derived_log IDs or as a set of arguments for the get_logs endpoint.",
            )
        if not values.get("key") and not values.get("equation"):
            raise ValueError("At least one of key or equation must be provided")
        return values


class RenameFieldRequest(BaseModel):
    project: str = Field(
        description="Name of the project the field belongs to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default="default",
        description="The context of the field to rename.",
        example="test-context",
    )
    old_field_name: str = Field(
        description="The current name of the field to rename.",
        example="score",
    )
    new_field_name: str = Field(
        description="The new name for the field.",
        example="score_new",
    )
