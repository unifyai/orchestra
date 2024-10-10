from typing import Any, Dict

from pydantic import BaseModel, Field


class ArtifactConfig(BaseModel):
    artifacts: Dict[str, Any] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be stored as artifacts within a project.",
        json_schema_extra={
            "example": {"dataset": "high-jump-data", "world-record": 2.45},
        },
    )
