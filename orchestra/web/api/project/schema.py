from typing import List, Optional

from pydantic import BaseModel, Field

# Import the template schemas from interface module
from orchestra.web.api.interface.schema import ProjectTemplateSchema


class FavoriteProjectIn(BaseModel):
    """Request model for creating a favorite project."""

    project: str = Field(description="The name of the project to favorite")
    position: int = Field(description="Position of the project in the favorites list")


class FavoriteProjectOut(FavoriteProjectIn):
    """Response model for favorite project data."""

    id: int = Field(description="Unique identifier for the favorite project")


class FavoriteProjectUpdate(BaseModel):
    """Request model for updating a favorite project."""

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
    is_versioned: bool = Field(
        description="Whether the project is versioned",
        default=False,
    )
    icon: Optional[str] = Field(
        None,
        description="Icon identifier for the project",
    )
    order: Optional[int] = Field(
        None,
        description="Position/order of the project in list",
    )
    description: Optional[str] = Field(
        None,
        description="Optional description of the project",
        max_length=256,
    )


class ProjectUpdate(BaseModel):
    """Request model for updating a project."""

    name: Optional[str] = Field(
        None,
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
    is_versioned: Optional[bool] = Field(
        None,
        description="Whether the project is versioned",
    )
    icon: Optional[str] = Field(
        None,
        description="Icon identifier for the project",
    )
    order: Optional[int] = Field(
        None,
        description="Position/order of the project in list",
    )
    description: Optional[str] = Field(
        None,
        description="Optional description of the project",
        max_length=256,
    )


class ProjectOut(BaseModel):
    """Response model for detailed project data."""

    id: int = Field(description="The unique identifier of the project")
    name: str = Field(description="The name of the project")
    description: Optional[str] = Field(None, description="Description of the project")
    icon: str = Field(description="Icon identifier for the project")
    is_versioned: bool = Field(description="Whether the project is versioned")
    created_at: Optional[str] = Field(description="When the project was created")
    updated_at: Optional[str] = Field(description="When the project was last updated")
    user_id: Optional[str] = Field(None, description="The ID of the user who owns the project")
    organization_id: Optional[int] = Field(None, description="The ID of the organization that owns the project")


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


class ProjectCommitRequest(BaseModel):
    commit_message: Optional[str] = None


class ProjectRollbackRequest(BaseModel):
    commit_hash: str


class ProjectCommitHistory(BaseModel):
    commit_hash: str
    commit_message: Optional[str] = None
    created_at: str
    prev_commit_hash: Optional[str] = None
    next_commit_hash: List[str] = []


class ExportProjectTemplateRequest(BaseModel):
    """Request to export a project template."""

    project: str
    interface_names: Optional[List[str]] = None  # If None, export all interfaces
    checkpoint: bool = False
    # Common template fields
    include_metadata: bool = True
    description: Optional[str] = None
    tags: List[str] = []
    template_name: Optional[str] = None


class ImportProjectTemplateRequest(BaseModel):
    """Request to import a project template."""

    project: str
    template: ProjectTemplateSchema  # Properly typed template instead of dict
    validate_first: bool = True
    auto_sanitize: bool = True
    overwrite_existing: bool = False
    interface_name_prefix: Optional[str] = None  # Prefix for imported interface names


class TabInfo(BaseModel):
    name: str
    icon: str
    order: int


class InterfaceInfo(BaseModel):
    name: str
    icon: str
    order: int
    tabs: List[TabInfo]


class ProjectTreeItem(BaseModel):
    project: str
    icon: str
    order: int
    interfaces: List[InterfaceInfo]
    favorite: bool = False
    position: Optional[int] = None


class TransferToOrganizationRequest(BaseModel):
    """Request model for transferring a personal project to an organization."""

    organization_id: int


class TransferResponse(BaseModel):
    """Response model for project transfer operations."""

    success: bool
    project_id: int
    project_name: str
    from_type: str  # "personal" or "organization"
    to_type: str  # "personal" or "organization"
    message: str
