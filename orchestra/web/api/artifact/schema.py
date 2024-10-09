from typing import Dict

from pydantic import BaseModel, Field


class ArtifactConfig(BaseModel):
    artifacts: Dict[str, str] = Field(
        description="Dictionary containing one or more key:value pairs that "
        "will be stored as artifacts.",
        json_schema_extra={
            "example": {"dataset": "my-dataset", "description": "..."},
        },
    )
