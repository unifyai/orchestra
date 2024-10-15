from typing import Any, Dict

from pydantic import BaseModel, Field


class DatasetArtifactConfig(BaseModel):
    artifacts: Dict[str, Any] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be stored as artifacts within a dataset.",
        json_schema_extra={
            "example": {"traffic": "production", "processed": False},
        },
    )
