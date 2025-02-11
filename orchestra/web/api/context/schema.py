"""Schema models for context management endpoints."""

from typing import List

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


class AddLogsToContextRequest(BaseModel):
    """Request model for adding existing logs to a context."""

    context_name: str = Field(
        ...,
        description="Name of the context to add logs to.",
        json_schema_extra={
            "example": "experiment1/trial1",
        },
    )
    log_ids: List[int] = Field(
        ...,
        description="List of log IDs to add to the context. At least one log ID must be provided.",
        min_items=1,
        json_schema_extra={
            "example": [123, 456, 789],
        },
    )
