from typing import Optional

from pydantic import BaseModel, Field


class FavoriteProjectIn(BaseModel):
    """Request model for creating a favorite project."""

    project: str = Field(description="The name of the project to favorite")
    icon: str = Field(description="Icon identifier for the favorite project")
    position: int = Field(description="Position of the project in the favorites list")


class FavoriteProjectOut(FavoriteProjectIn):
    """Response model for favorite project data."""

    id: int = Field(description="Unique identifier for the favorite project")


class FavoriteProjectUpdate(BaseModel):
    """Request model for updating a favorite project."""

    icon: Optional[str] = Field(
        None,
        description="Icon identifier for the favorite project",
    )
    position: Optional[int] = Field(
        None,
        description="Position of the project in the favorites list",
    )


class ProjectConfig(BaseModel):
    name: str = Field(
        description="A unique, user-defined name used when referencing  "
        "the project.",
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_\-/]+$",
        json_schema_extra={
            "example": "eval-project",
            "pattern": "^[a-zA-Z0-9_\\-/]+$",
            "pattern_description": "Only letters, numbers, underscores, slashes, and hyphens are allowed",
        },
    )


class ShareProjectRequest(BaseModel):
    """Request model for sharing a project between users."""

    from_user_id: str
    to_user_id: str
    project_name: str


class DuplicateProjectRequest(BaseModel):
    """Request model for duplicating a project."""

    from_user_id: str
    from_project_name: str
    to_user_id: str
    new_project_name: str
