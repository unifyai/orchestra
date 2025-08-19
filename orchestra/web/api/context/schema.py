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
    unique_keys: Optional[Dict[str, str]] = Field(
        default=None,
        description="Composite unique key definition. Keys are column names, values are types ('counting' for auto-increment, 'str', 'int', 'float', 'bool', 'datetime', 'time', 'date', 'timedelta', 'dict', 'list').",
        example={
            "department_id": "counting",
            "first_name": "str",
            "company_id": "counting",
            "last_name": "str",
        },
    )

    @field_validator("unique_keys")
    @classmethod
    def validate_unique_keys(cls, v):
        """Validate unique keys."""
        if v is None:
            return v

        if not v:  # Empty dict
            raise ValueError(
                "unique_keys cannot be an empty dict. Use None to disable unique keys.",
            )

        # Valid types for composite keys
        from orchestra.web.api.log.python2SQL.constants import VALID_COMPOSITE_KEY_TYPES

        allowed_types = VALID_COMPOSITE_KEY_TYPES

        counting_columns = []
        for col_name, col_type in v.items():
            # Validate column name
            if not isinstance(col_name, str):
                raise ValueError("All column names must be strings")
            if not re.match(r"^[a-zA-Z0-9_]+$", col_name):
                raise ValueError(
                    f"Column name '{col_name}' must contain only alphanumeric characters and underscores",
                )

            # Validate type
            if col_type not in allowed_types:
                raise ValueError(
                    f"Invalid type '{col_type}' for column '{col_name}'. Allowed types: {allowed_types}",
                )

            if col_type == "counting":
                counting_columns.append(col_name)

        # If there are counting columns, they must form a valid hierarchy (ordered list)
        # For now, we'll preserve the order as given in the dict (Python 3.7+ preserves insertion order)

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
    prev_commit_hash: Optional[str] = None
    next_commit_hash: List[str] = []
