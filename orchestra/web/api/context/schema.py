"""Schema models for context management endpoints."""

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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
    unique_column_ids: Optional[List[str]] = Field(
        default=None,
        description="List of unique column names for nested unique IDs. Leftmost is most major, rightmost is most minor. If None or empty, no unique IDs are generated.",
        example=["task_id", "instance_id"],
    )

    @field_validator("unique_column_ids")
    @classmethod
    def validate_unique_column_ids(cls, v):
        """Validate unique column IDs."""
        if v is None:
            return v

        if not v:  # Empty list
            raise ValueError(
                "unique_column_ids cannot be an empty list. Use None to disable unique IDs.",
            )

        # Check for duplicates
        if len(v) != len(set(v)):
            raise ValueError("unique_column_ids cannot contain duplicate names")

        # Validate each column name
        for name in v:
            if not isinstance(name, str):
                raise ValueError("All column names must be strings")
            if not re.match(r"^[a-zA-Z0-9_]+$", name):
                raise ValueError(
                    f"Column name '{name}' must contain only alphanumeric characters and underscores",
                )

        return v


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


class RenameContextRequest(BaseModel):
    """Request model for renaming an existing context."""

    name: str = Field(
        ...,
        description="New name for the context, must meet naming rules",
        json_schema_extra={
            "example": "experiment2/trial3",
        },
    )


class ContextRollbackRequest(BaseModel):
    version: Optional[int] = None
    commit_hash: Optional[str] = None


class ContextCommit(BaseModel):
    commit_message: Optional[str] = None


class ContextRollback(BaseModel):
    commit_hash: str


class ContextCommitHistory(BaseModel):
    commit_hash: str
    commit_message: Optional[str] = None
    created_at: str
    type: Literal["project", "context"]
