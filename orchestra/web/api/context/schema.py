"""Schema models for context management endpoints."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ContextCreateRequest(BaseModel):
    """Request model for creating a new context within a project."""

    name: str = Field(
        ...,
        description="Context name, can be nested using '/' (e.g., 'parent/child'). "
        "Must contain only alphanumeric characters, underscores, and hyphens.",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    description: str | None = Field(
        default=None,
        description="Optional description of the context",
        example="Context for experiment 1 trial 1",
    )
    is_versioned: bool = Field(
        default=False,
        description="Whether the context should be versioned. If True, the context will be versioned and mutable. ",
        example=True,
    )
    allow_duplicates: bool = Field(
        default=True,
        description="Whether duplicate log entries are allowed in this context. If False, attempts to add duplicate logs will be ignored.",
        example=True,
    )


class AddLogsToContextRequest(BaseModel):
    """Request model for adding existing logs to a context."""

    context_name: str = Field(
        ...,
        description="Name of the context to add logs to.",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    log_ids: Optional[List[int]] = Field(
        None,
        description="List of log IDs to add to the context. At least one log ID must be provided.",
        min_items=1,
        json_schema_extra={
            "example": [123, 456, 789],
        },
    )
    log_args: Optional[Dict[str, Any]] = Field(
        None,
        description="Dictionary of arguments (e.g. filter_expr) to select logs by criteria.",
        json_schema_extra={"example": {"filter_expr": "metric > 0.9"}},
    )
    copy: bool = Field(
        default=False,
        description="If True, a copy of each log is created and then added to the context. If False, the existing log associations are simply used.",
    )


class CreateColumnsRequest(BaseModel):
    """Request model for creating columns in a context."""

    columns: Dict[str, Optional[str]] = Field(
        ...,
        description="Dictionary mapping column names to their explicit types (or None for auto-detection).",
        json_schema_extra={
            "example": {"accuracy": "float", "model_name": "str", "iteration": None},
        },
    )


class RenameContextRequest(BaseModel):
    """Request model for renaming an existing context."""

    name: str = Field(
        ...,
        description="New name for the context, must meet naming rules",
        json_schema_extra={
            "example": "experiment2/trial3",
        },
    )
