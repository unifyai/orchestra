from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator

from orchestra.web.api.context.schema import ContextCreateRequest


class RowIDs(BaseModel):
    names: List[str] = Field(
        description="List of composite unique key column names. For single columns, this will be a list with one element. For composite keys, this contains all column names in the key.",
        example=["department_id", "first_name"],
    )
    ids: List[List[Any]] = Field(
        description="List of composite key value lists corresponding to each log event. "
        "Each inner list contains the values for the columns specified in 'names'. Values can be of any type (int, str, etc.).",
        example=[[0, "Alice"], [1, "Bob"], [2, "Charlie"]],
    )


class CreateLogsResponse(BaseModel):
    log_event_ids: List[int] = Field(
        description="List of created log event IDs in the order they were created.",
        example=[123, 124, 125],
    )
    row_ids: RowIDs = Field(
        description="Object containing the names of the unique ID columns and the corresponding nested row IDs.",
        example={"names": ["row_id"], "ids": [[0], [1], [2]]},
    )
    auto_counting: Dict[str, List[Any]] = Field(
        description="Dictionary mapping auto-counting column names to their generated/provided values for each created log. "
        "Empty dict when no auto-counting columns are configured.",
        example={"message_id": [0, 1, 2], "exchange_id": [0, 1, 2]},
    )


class EnumType(BaseModel):
    type: Literal["enum"]
    values: Optional[List[str]] = None
    restrict: Optional[bool] = False
    description: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional description for the enum field type",
    )

    class Config:
        schema_extra = {
            "description": "Defines an enum field type. Omit 'values' to create an open enum that auto-seeds itself on first write.",
        }


class StandardFieldDefinition(BaseModel):
    """
    Defines a standard field with type information and behavioral flags.

    The `type` field specifies the data type of the field.
    The `mutable` flag controls whether the field can later be modified via update endpoints.
    The `unique` flag controls whether the field can only have one value per log.
    The `description` field provides an optional human-readable description of the field.
    """

    type: str
    mutable: bool = Field(
        default=False,
        description="If true, entries under this field can be updated via update endpoints; otherwise they are immutable after creation (default false).",
    )
    unique: bool = False
    description: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional description for the field definition",
    )


class CreateLogConfig(BaseModel):
    project: str = Field(
        description="Name of the project the stored entries will be associated to.",
        json_schema_extra={
            "example": "eval-project",
        },
    )
    context: Union[ContextCreateRequest, str, None] = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1'). "
        "Can be a string (which will be interpreted with description=None and is_versioned=False) "
        "or a ContextCreateRequest object.",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    params: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Can be either a single dictionary or a list of dictionaries "
        "for batch processing. When using lists for both params and entries, their lengths must match. "
        "Values must be JSON serializable. If a `explicit_types` dictionary is present, its values "
        "will override the inferred types of the entries. The explicit_types dictionary can also specify if a field is mutable via a 'mutable' boolean flag, or unique via a 'unique' boolean flag. "
        "For enum types, use the EnumType model with 'values' list and optional 'restrict' flag. Omit 'values' to create an open enum (auto-seeding). "
        "For contexts with nested unique IDs, parent ID values for the leftmost N-1 unique columns can be supplied as normal param keys. "
        "The rightmost column is always auto-incremented. For example, if unique columns are ['user', 'session', 'step'], "
        "you can provide 'user' and 'session' values in params, and 'step' will be auto-generated.",
        json_schema_extra={
            "examples": [
                {
                    "system-prompt": "...",
                    "function_definition": "...",
                    "explicit_types": {
                        "system-prompt": {
                            "type": "str",
                            "mutable": True,
                            "unique": False,
                            "description": "The system prompt used for generation",
                        },
                        "category": {
                            "type": "enum",
                            "values": ["A", "B", "C"],
                            "restrict": True,
                            "description": "Classification category",
                        },
                        "status": {
                            "type": "enum",
                            "restrict": False,
                        },  # Open enum with no values
                    },
                },
                [
                    {"system-prompt": "prompt1"},
                    {"system-prompt": "prompt2"},
                ],
                # Example with nested unique IDs - providing parent IDs directly
                [
                    {"user": 100, "session": 4, "system-prompt": "prompt1"},
                    {"user": 100, "session": 4, "system-prompt": "prompt2"},
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
        "its values will override the inferred types of the entries. The explicit_types dictionary can also specify if a field is mutable via a 'mutable' boolean flag, or unique via a 'unique' boolean flag. "
        "For enum types, use the EnumType model with 'values' list and optional 'restrict' flag. Omit 'values' to create an open enum (auto-seeding). "
        "For contexts with nested unique IDs, parent ID values for the leftmost N-1 unique columns can be supplied as normal entry keys. "
        "The rightmost column is always auto-incremented. For example, if unique columns are ['user', 'session', 'step'], "
        "you can provide 'user' and 'session' values in entries, and 'step' will be auto-generated.",
        json_schema_extra={
            "examples": [
                {
                    "input": "...",
                    "score-test-1": "...",
                    "explicit_types": {
                        "input": {
                            "type": "Image",
                            "mutable": True,
                            "unique": True,
                            "description": "Input image for processing",
                        },
                        "status": {
                            "type": "enum",
                            "values": ["pending", "completed", "failed"],
                            "restrict": True,
                            "description": "Processing status",
                        },
                        "tag": {"type": "enum"},  # Open enum with no values
                    },
                },
                [
                    {"input": "test1", "score": 0.8},
                    {"input": "test2", "score": 0.9},
                ],
                # Example with nested unique IDs - providing parent IDs directly
                [
                    {"user": 100, "session": 4, "data": "step1"},
                    {"user": 100, "session": 4, "data": "step2"},
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
    context: Union[ContextCreateRequest, str, None] = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1'). "
        "Can be a string (which will be interpreted with description=None and is_versioned=False) "
        "or a ContextCreateRequest object.",
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
    derived: bool = Field(
        default=True,
        description="Whether to create derived logs (True) or static entries in base logs (False).",
        example=False,
    )


class UpdateLogRequest(BaseModel):
    logs: Union[List[int], Dict[str, Any]] = Field(
        description='List of log IDs or a dict of filter arguments to select logs. Filter dicts are passed as key:value pairs (e.g. `{"status": "done", "user_id": 12}`).',
        json_schema_extra={
            "examples": [[123, 456, 789], {"status": "done", "user_id": 12}],
        },
    )
    project: Optional[str] = Field(
        default=None,
        description="Name of the project. Required when using filter dict in `logs`. Omit when passing a list of IDs.",
        example="eval-project",
    )
    context: Union[
        ContextCreateRequest,
        str,
        List[Union[ContextCreateRequest, str]],
        None,
    ] = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1'). "
        "Can be a string (which will be interpreted with description=None and is_versioned=False) "
        "or a ContextCreateRequest object. Required when using filter dict in `logs` if project is not provided.",
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
                "explicit_types": {
                    "system-prompt": {
                        "type": "str",
                        "mutable": True,
                        "description": "System prompt for the model",
                    },
                    "category": {
                        "type": "enum",
                        "values": ["A", "B", "C"],
                        "restrict": False,
                        "description": "Task category",
                    },
                    "priority": {"type": "enum"},  # Open enum with no values
                },
            },
        },
    )
    entries: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary or list of dictionaries of key-value entry pairs to add or update in the logs. "
        "Supports nested path syntax using dot notation for object properties (e.g., 'metadata.author') "
        "and bracket notation for array indices (e.g., 'results[0]'). "
        "Complex paths like 'results[0].scores.accuracy' are also supported for deep updates.",
        json_schema_extra={
            "example": {
                "input": "...",
                "score-test-1": "...",
                "explicit_types": {
                    "input": {
                        "type": "Image",
                        "mutable": True,
                        "description": "Input data for processing",
                    },
                    "status": {
                        "type": "enum",
                        "values": ["pending", "completed", "failed"],
                        "restrict": True,
                        "description": "Current processing status",
                    },
                    "label": {"type": "enum"},  # Open enum with no values
                },
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
    delete_empty_fields: bool = Field(
        default=True,
        description="Whether to delete fields that become empty after log deletion",
        example=True,
    )


class DeleteLogEntryRequest(BaseModel):
    project: str = Field(
        description="Name of the project the logs belong to.",
        example="eval-project",
    )
    context: str | None = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1').",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    ids_and_fields: List[
        Tuple[Union[int, List[int], Dict[str, Any], None], Union[None, str, List[str]]]
    ] = Field(
        description="List of tuples of log ID(s) and field(s) to delete, "
        "either as an individual item or a list of items. A log ID of None indicates "
        "that the field should be deleted from all logs. Can also use a dict of filter arguments to select logs. "
        "The filter dict should be a key:value pair where the key is the field to filter on and the value is the value to filter on.",
        example=[
            (123, "score"),
            ([456, 457], ["score", "response"]),
            ([458, 459, 460], "response"),
            ({"score": "100"}, None),
            (None, "score"),
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
    delete_empty_logs: bool = Field(
        default=False,
        description="Whether to delete logs which end up being empty as a result of the field deletion.",
        example=True,
    )
    delete_empty_fields: bool = Field(
        default=True,
        description="Whether to delete fields that have no data after log deletion.",
        example=True,
    )


class UpdateDerivedEntriesConfig(BaseModel):
    project: str = Field(
        description="Name of the project these derived logs belong to.",
        example="eval-project",
    )
    context: Union[ContextCreateRequest, str, None] = Field(
        default=None,
        description="Optional context path to update for the logs. "
        "Can use '/' for nested contexts (e.g. 'training/batch1'). "
        "Can be a string (which will be interpreted with description=None and is_versioned=False) "
        "or a ContextCreateRequest object.",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
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
        if not values.get("key"):
            raise ValueError("key must be provided")
        return values


class RenameFieldRequest(BaseModel):
    project: str = Field(
        description="Name of the project the field belongs to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default="",
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


class GetLogsMetricRequest(BaseModel):
    # A single key or multiple keys
    key: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Single key string or a list of keys.",
    )
    # Optional dictionary mapping each key to its metric, e.g. {"score":"mean","runtime":"sum"}
    metrics: Optional[Dict[str, str]] = Field(
        default=None,
        description="Optional per-key metrics mapping. If provided, overrides the path metric for those keys.",
    )
    filter_expr: Optional[Union[str, Dict[str, str]]] = Field(
        default=None,
        description="Expression to filter logs (string or key->expr dict).",
    )
    from_ids: Optional[Union[str, Dict[str, str]]] = Field(
        default=None,
        description="Log IDs to include (string or key->IDs dict).",
    )
    exclude_ids: Optional[Union[str, Dict[str, str]]] = Field(
        default=None,
        description="Log IDs to exclude (string or key->IDs dict).",
    )
    context: Optional[str] = Field(
        default=None,
        description="Context name (string).",
    )
    group_by: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Field(s) to group by when computing metrics. Can be a single field name or a list of field names for nested grouping.",
    )


class CreateFieldsRequest(BaseModel):
    """
    Request model for creating fields in a project context.

    Fields can be defined with various properties including mutability. The `mutable` flag
    determines whether field values can be updated after creation via update endpoints.
    Immutable fields (mutable=False) provide data integrity guarantees once set.
    """

    project: str = Field(
        description="Name of the project the fields belong to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context path for the fields.",
        example="experiment1/trial1",
    )
    fields: Dict[str, Union[StandardFieldDefinition, EnumType, str, None]] = Field(
        description="Dictionary mapping field names to their type definitions.",
        example={
            "score": "int",
            "response": None,
            "email": {
                "type": "str",
                "unique": True,
                "description": "User email address",
            },
            "comment": {
                "type": "str",
                "mutable": True,
                "description": "User comment",
            },
        },
    )
    backfill_logs: bool = Field(
        default=True,
        description="Whether to backfill existing logs in the context with None values for the new fields. "
        "When True, all existing logs will get the new fields with None values, ensuring all rows can "
        "participate in derived equations without errors.",
        example=True,
    )


class DeleteFieldsRequest(BaseModel):
    project: str = Field(
        description="Name of the project the fields belong to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context path for the fields.",
        example="experiment1/trial1",
    )
    fields: List[str] = Field(
        description="List of field names to delete.",
        example=["score", "response"],
    )


class JoinLogsRequest(BaseModel):
    pair_of_args: List[Dict[str, Any]] = Field(
        ...,
        description="Two sets of filtering criteria for logs to join",
        example=[
            {"context": "context_a", "filter_expr": "user_id == 1"},
            {"context": "context_b", "filter_expr": "user_id == 2"},
        ],
    )
    join_expr: str = Field(
        ...,
        description="SQL expression for join condition using aliases A and B",
        example="A.user_id == B.user_id",
    )
    mode: str = Field(
        ...,
        description="Join type: 'inner', 'left', 'right', or 'outer'",
        example="inner",
    )
    new_context: str = Field(
        ...,
        description="Name for the new context where joined logs will be stored",
        example="Derived/A_B",
    )
    columns: Optional[Union[Dict[str, str], List[str]]] = Field(
        default=None,
        description=(
            "Optional column specification for the joined result. "
            "Can be either:\n"
            "1. A dictionary mapping source columns to aliases (only supported when copy=True): "
            "   {'A.user_id': 'user_identifier', 'B.score': 'user_score'}\n"
            "2. A list of source columns to include (required format when copy=False): "
            "   ['A.user_id', 'A.score', 'B.category']\n"
            "Note: When copy=False (pass-by-reference), aliases are not supported and the original "
            "column names will be preserved. Use the list format in this case.\n"
            "If omitted, all columns will be selected and prefixed with 'A_' or 'B_'."
        ),
        example={"A.user_id": "user_identifier", "B.score": "user_score"},
    )
    project: str = Field(..., description="Name of the project")
    copy: bool = Field(
        default=True,
        description="If True, a copy of each log is created and then added to the context. If False, the existing log associations are simply used. If omitted, defaults to True.",
    )
