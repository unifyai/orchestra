"""Schema models for context management endpoints."""

from typing import Any, Dict

from pydantic import BaseModel, Field


class ContextArtifactCreateRequest(BaseModel):
    """Request model for creating a new context artifact within a context."""

    artifacts: Dict[str, Any] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be stored as artifacts within a project.",
        example={"dataset": "high-jump-data", "world-record": 2.45},
    )
