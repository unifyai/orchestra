from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing  "
        "the project.",
        json_schema_extra={"example": "eval-project"},
    )
