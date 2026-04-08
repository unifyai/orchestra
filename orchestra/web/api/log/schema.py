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
        default=True,
        description="If true, entries under this field can be updated via update endpoints; otherwise they are immutable after creation (default true).",
    )
    unique: bool = False
    description: Optional[str] = Field(
        None,
        max_length=256,
        description="Optional description for the field definition",
    )


class JsonSchemaFieldDefinition(BaseModel):
    """
    Accepts a full JSON Schema field definition (e.g., from Pydantic's model_json_schema()).

    This allows passing standard JSON Schema with all its features:
    - Standard types: "string", "integer", "number", "boolean", "array", "object", "null"
    - $ref and $defs for complex/nested types
    - anyOf, oneOf, allOf for union types
    - items for array element types
    - properties for object property types
    - format, minimum, maximum, pattern, etc. for constraints
    - title, description for metadata

    Orchestra normalizes JSON Schema types to internal types:
    - "string" -> "str"
    - "integer" -> "int"
    - "number" -> "float"
    - "boolean" -> "bool"
    - "array" -> "list"
    - "object" -> "dict"
    - "null" -> "NoneType"

    The full schema is preserved for validation against logged values.
    """

    model_config = {"extra": "allow"}

    # Optional top-level type (may be absent for $ref or anyOf schemas)
    type: Optional[str] = None

    @model_validator(mode="after")
    def validate_is_json_schema(self):
        """Validate that this looks like a JSON Schema (has recognizable schema keys)."""
        # Get all fields including extra
        data = self.model_dump()

        # A JSON Schema typically has one of these keys
        json_schema_indicators = {
            "type",
            "$ref",
            "anyOf",
            "oneOf",
            "allOf",
            "items",
            "properties",
            "enum",
            "const",
            "$defs",
            "definitions",
        }

        has_indicator = any(key in data for key in json_schema_indicators)
        if not has_indicator:
            raise ValueError(
                "JsonSchemaFieldDefinition requires at least one JSON Schema key "
                f"(e.g., type, $ref, anyOf). Got keys: {list(data.keys())}",
            )
        return self


class CreateLogConfig(BaseModel):
    project_name: str = Field(
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
    entries: Union[Dict[str, Any], List[Dict[str, Any]]] = Field(
        default=dict(),
        description="Dictionary containing one or more key:value pairs that "
        "will be logged into the platform. Can be either a single dictionary or a list of dictionaries "
        "for batch processing. "
        "Values must be JSON serializable. If an `explicit_types` dictionary is present, "
        "its values will override the inferred types of the entries. The explicit_types dictionary can also specify if a field is mutable via a 'mutable' boolean flag, or unique via a 'unique' boolean flag. "
        "For enum types, use the EnumType model with 'values' list and optional 'restrict' flag. Omit 'values' to create an open enum (auto-seeding). "
        "If `infer_untyped_fields` is set to True, fields with type 'Any' (untyped) will have their type inferred from the logged values and updated, locking in the type. "
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
                # Example with infer_untyped_fields - locks in types for 'Any' fields
                {
                    "score": 42,
                    "infer_untyped_fields": True,
                },
            ],
        },
    )
    recompute_derived: bool = Field(
        default=False,
        description="If True, recompute derived columns for the newly created logs "
        "using active ActiveDerivedLog templates. Suitable for small batches; "
        "for large ingestion workflows, leave False and rely on periodic backfill.",
    )


class CreateDerivedEntriesConfig(BaseModel):
    project_name: str = Field(
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
    project_name: Optional[str] = Field(
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
    project_name: str = Field(
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
    project_name: str = Field(
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
    project_name: str = Field(
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

    project_name: str = Field(
        description="Name of the project the fields belong to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context path for the fields.",
        example="experiment1/trial1",
    )
    fields: Dict[
        str,
        Union[EnumType, StandardFieldDefinition, JsonSchemaFieldDefinition, str, None],
    ] = Field(
        description="Dictionary mapping field names to their type definitions. "
        "Supports multiple formats:\n"
        "- Simple string: 'str', 'int', 'float', 'bool', 'list', 'dict', 'datetime', 'image', etc.\n"
        "- JSON Schema types: 'string', 'integer', 'number', 'boolean', 'array', 'object'\n"
        "- StandardFieldDefinition: {'type': 'str', 'mutable': True, 'unique': False}\n"
        "- Full JSON Schema: {'type': 'string', 'format': 'date-time'} or {'$ref': '#/$defs/MyModel'}\n"
        "- EnumType: {'type': 'enum', 'values': ['a', 'b', 'c']}\n"
        "- None: Untyped field (accepts any value)",
        example={
            "score": "int",
            "response": None,
            "email": {
                "type": "str",
                "unique": True,
                "description": "User email address",
            },
            "timestamp": {
                "type": "string",
                "format": "date-time",
                "description": "ISO-8601 timestamp",
            },
            "status": {
                "type": "enum",
                "values": ["pending", "approved", "rejected"],
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
    project_name: str = Field(
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


class UpdateFieldRequest(BaseModel):
    project_name: str = Field(
        description="Name of the project the field belongs to.",
        example="eval-project",
    )
    context: Optional[str] = Field(
        default="",
        description="The context of the field to update.",
        example="test-context",
    )
    field_name: str = Field(
        description="The name of the field to update.",
        example="score",
    )
    description: Optional[str] = Field(
        ...,
        max_length=256,
        description="Field description. This is the only supported field update. Use null to clear the description.",
        example="Human-readable score for this log entry",
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
    project_name: str = Field(..., description="Name of the project")
    copy: bool = Field(
        default=True,
        description="If True, a copy of each log is created and then added to the context. If False, the existing log associations are simply used. If omitted, defaults to True.",
    )


class JoinQueryRequest(BaseModel):
    """Execute a join and query/reduce the result in a single operation.

    Combines the join specification (same as JoinLogsRequest minus
    ``new_context`` / ``copy``) with post-join query parameters (filter,
    sort, paginate, group, reduce).  No temporary context is created.

    Two modes of operation:

    * **Row mode** (``metric`` is ``None``): returns paginated rows from the
      joined result, optionally filtered and sorted.  Accepts ``sorting``,
      ``limit``, ``offset``, and ``filter_expr``.
    * **Reduce mode** (``metric`` + ``key`` provided): returns aggregated
      metric values, optionally grouped via ``group_by``.  Accepts
      ``filter_expr`` and ``group_by``.

    Not all parameters apply to both modes.  Invalid combinations are
    rejected with an actionable error message pointing to the correct
    endpoint or parameter combination.
    """

    # --- Join spec ---
    pair_of_args: List[Dict[str, Any]] = Field(
        ...,
        description="Two sets of filtering criteria for logs to join. "
        "Each dict may contain 'context', 'filter_expr', 'from_ids', 'exclude_ids'.",
    )
    join_expr: str = Field(
        ...,
        description="Join condition expression using aliases A and B, "
        "e.g. 'A.user_id == B.user_id'.",
    )
    mode: str = Field(
        default="inner",
        description="Join type: 'inner', 'left', 'right', or 'outer'.",
    )
    columns: Optional[Union[Dict[str, str], List[str]]] = Field(
        default=None,
        description="Column specification for the joined result. "
        "Dict maps 'A.col'→alias; list uses original names. "
        "If omitted, all columns from both sides are merged.",
    )
    project_name: str = Field(..., description="Name of the project.")

    # --- Post-join query ---
    filter_expr: Optional[str] = Field(
        default=None,
        description="Boolean expression to filter the joined rows "
        "(uses output column names). Applies to both modes.",
    )
    sorting: Optional[str] = Field(
        default=None,
        description="Row mode only. JSON-encoded dict mapping column names "
        "to sort directions ('ascending'/'descending'). Note: sort comparisons "
        "are text-based; numeric columns compare lexicographically.",
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000,
        description="Row mode only. Maximum number of rows to return.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Row mode only. Number of rows to skip.",
    )
    group_by: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Reduce mode only. Field(s) in the joined result to "
        "group by before aggregation.",
    )

    # --- Reduction ---
    metric: Optional[str] = Field(
        default=None,
        description="Reduce mode. Aggregation metric: count, sum, mean, "
        "var, std, min, max, median, mode. Must be paired with ``key``.",
    )
    key: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Reduce mode. Column(s) to aggregate. "
        "Must be paired with ``metric``.",
    )

    @model_validator(mode="after")
    def _validate_mode_args(self):
        is_reduce = self.metric is not None
        if is_reduce:
            if self.key is None:
                raise ValueError(
                    "`key` is required when `metric` is set. "
                    "Specify the column(s) to aggregate, e.g. key='amount'.",
                )
            if self.sorting is not None:
                raise ValueError(
                    "`sorting` only applies to row mode (metric=None). "
                    "Reduce mode returns grouped aggregates which have no "
                    "row order. To sort rows before aggregating, apply a "
                    "pre-join filter via pair_of_args instead, or call "
                    "POST /logs/join_query without `metric` to get sorted "
                    "rows, then POST /logs/metric/{metric} on those results.",
                )
            if self.limit is not None:
                raise ValueError(
                    "`limit` only applies to row mode (metric=None). "
                    "Reduce mode returns one value per group — all groups "
                    "are always returned. To limit input rows before "
                    "aggregating, use `filter_expr` or pre-join filters "
                    "in `pair_of_args`.",
                )
            if self.offset != 0:
                raise ValueError(
                    "`offset` only applies to row mode (metric=None). "
                    "Reduce mode returns one value per group — pagination "
                    "does not apply. To paginate joined rows, call this "
                    "endpoint without `metric` and use `limit`/`offset`.",
                )
        else:
            if self.key is not None:
                raise ValueError(
                    "`key` only applies to reduce mode. Set `metric` "
                    "(e.g. 'count', 'sum', 'mean') together with `key` "
                    "to aggregate, or remove `key` to use row mode.",
                )
            if self.group_by is not None:
                raise ValueError(
                    "`group_by` only applies to reduce mode. Set `metric` "
                    "and `key` together with `group_by` to get grouped "
                    "aggregates. For row mode, use `sorting` to order "
                    "results and `limit`/`offset` to paginate.",
                )
        return self


class QueryLogsPostBody(BaseModel):
    """
    Request body for POST /logs/query endpoint.

    Accepts the same parameters as GET /logs, but via request body instead of query parameters.
    This is useful for queries with large filter expressions or base64-encoded images that
    would exceed URL length limits.

    Example with image embedding:
    ```json
    {
        "project_name": "my-project",
        "filter_expr": "cosine(image_embedding, embed_image('data:image/png;base64,iVBORw0KG...')) < 0.3"
    }
    ```
    """

    project_name: str = Field(description="Project name", example="eval-project")
    context: Optional[str] = Field(None, description="Context name", example="training")
    column_context: Optional[str] = Field(
        None,
        description="The context (prepending '/' seperated field names) from which to retrieve the logs.",
        example="subjects/science/physics",
    )
    filter_expr: Optional[str] = Field(
        None,
        description="Boolean string to filter entries. Supports embed_image() for image similarity queries.",
        example="len(output) > 200 and temperature == 0.5",
    )
    sorting: Optional[str] = Field(
        None,
        description='JSON-encoded dict mapping either static column names (e.g. `timestamp`) or full Python2SQL expressions (e.g. `cosine(embed(\'search text\'), embedding_vector)`) to sort directions (`"ascending"` or `"descending"`). The first key is the primary sort field; subsequent keys break ties.',
        example='{"timestamp": "descending", "round(score, 2)": "ascending"}',
    )
    group_sorting: Optional[str] = Field(
        None,
        description="Sorting configuration for groups when using group_by",
        example={
            "entries/student": {
                "field": "score",
                "direction": "descending",
                "metric": "mean",
            },
        },
    )
    from_ids: Optional[Any] = Field(
        None,
        description="Log IDs to include",
        example="0&1&2",
    )
    exclude_ids: Optional[Any] = Field(
        None,
        description="Log IDs to exclude",
        example="0&1&2",
    )
    from_fields: Optional[str] = Field(
        None,
        description="Fields to include",
        example="score&response",
    )
    exclude_fields: Optional[str] = Field(
        None,
        description="Fields to exclude",
        example="score&response",
    )
    limit: Optional[int] = Field(None, ge=1, le=1000)
    offset: int = Field(0, ge=0)
    group_by: Optional[List[str]] = Field(
        None,
        description="Fields to group by",
        example=["model", "temperature"],
    )
    group_limit: Optional[int] = Field(
        None,
        description="Maximum number of groups to return at each level",
        ge=1,
    )
    group_offset: int = Field(
        0,
        description="Number of groups to skip at each level",
        ge=0,
    )
    group_depth: Optional[int] = Field(
        None,
        description="Maximum depth of nested groups to return",
    )
    nested_groups: bool = Field(
        True,
        description="If True, groups are returned as a nested structure",
    )
    groups_only: bool = Field(
        False,
        description="If True, only return groups without full logs list",
    )
    return_timestamps: bool = Field(
        False,
        description="When groups_only is True, return timestamps with log IDs",
    )
    return_ids_only: bool = Field(
        False,
        description="If True, return only log IDs instead of full entries",
    )
    randomize: bool = Field(
        False,
        description="If true, return logs in a deterministic random order",
    )
    seed: Optional[str] = Field(
        "42",
        description="Seed for deterministic random ordering",
    )
    value_limit: Optional[int] = Field(
        None,
        description="Maximum number of characters to return for string values",
    )
    group_threshold: Optional[int] = Field(
        None,
        description="When set, entries that appear in at least this many logs will be grouped together",
    )


class AtomicFieldUpdateRequest(BaseModel):
    """Request model for atomic field operations that are race-safe under concurrent updates.

    This endpoint supports two modes:
    1. Update mode (default): Updates an existing log entry by log_id
    2. Upsert mode: When project/context/unique_keys/initial_data are provided,
       finds or creates a log entry by unique keys, then applies the atomic operation.

    Upsert mode uses advisory locks to handle concurrent first inserts safely.
    """

    operation: str = Field(
        description="Atomic operation to apply. Supported formats: +N, -N, *N, /N where N is a number.",
        example="+1",
    )
    # Optional fields for upsert mode
    field: Optional[str] = Field(
        default=None,
        description="(Upsert mode) Name of the numeric field to update atomically.",
        example="cumulative_spend",
    )
    project: Optional[str] = Field(
        default=None,
        description="(Upsert mode) Name of the project.",
        example="Assistants",
    )
    context: Optional[str] = Field(
        default=None,
        description="(Upsert mode) Context path for the log.",
        example="42/7/Spending/Monthly",
    )
    unique_keys: Optional[Dict[str, str]] = Field(
        default=None,
        description="(Upsert mode) Unique key configuration for the context. Maps key names to types (str, int, float).",
        example={"_assistant_id": "str", "month": "str"},
    )
    initial_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="(Upsert mode) Data to use when creating a new log entry. Must include all unique key values.",
        example={"_assistant_id": "123", "month": "2026-01", "_org_id": 456},
    )
    add_to_all_context: bool = Field(
        default=False,
        description="(Upsert mode) If true, also adds the log to the 'All/*' archive context.",
    )


class AtomicFieldUpdateResponse(BaseModel):
    """Response from atomic field update operation."""

    new_value: float = Field(
        description="The new value of the field after the operation.",
    )
    log_id: Optional[int] = Field(
        default=None,
        description="ID of the log entry (included in upsert mode).",
    )
    created: Optional[bool] = Field(
        default=None,
        description="True if a new log was created (upsert mode only).",
    )
    mirrored_contexts: Optional[List[str]] = Field(
        default=None,
        description="List of archive contexts the log was mirrored to (upsert mode only).",
    )
