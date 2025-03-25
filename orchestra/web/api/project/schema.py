from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing  "
        "the project.",
        json_schema_extra={"example": "eval-project"},
    )


class ShareProjectRequest(BaseModel):
    """Request model for sharing a project between users."""

    from_user_id: str
    to_user_id: str
    project_name: str
