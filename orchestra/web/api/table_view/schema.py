"""Pydantic schemas for Table View API."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# =============================================================================
# Input Schemas
# =============================================================================


class ColumnConfig(BaseModel):
    """Configuration for table columns."""

    visible: Optional[List[str]] = Field(
        None,
        description="Columns to show (null = all columns visible)",
    )
    hidden: Optional[List[str]] = Field(
        None,
        description="Columns to hide (alternative to visible)",
    )
    order: Optional[List[str]] = Field(
        None,
        description="Column display order",
    )
    widths: Optional[Dict[str, int]] = Field(
        None,
        description="Column widths in pixels: {column_name: width}",
    )

    @model_validator(mode="after")
    def validate_visible_hidden_exclusive(self):
        """Ensure visible and hidden are mutually exclusive."""
        if self.visible is not None and self.hidden is not None:
            raise ValueError(
                "Cannot specify both 'visible' and 'hidden' - use one or the other",
            )
        return self


class TableConfigInput(BaseModel):
    """Table configuration input from user."""

    columns: Optional[ColumnConfig] = Field(
        None,
        description="Column visibility, ordering, and sizing",
    )
    row_limit: Optional[int] = Field(
        100,
        description="Maximum rows to display",
        ge=1,
        le=10000,
    )
    sort_by: Optional[str] = Field(
        None,
        description="Column to sort by initially (invalid columns are ignored at render time)",
    )
    sort_order: Optional[str] = Field(
        None,
        description="Sort order: asc or desc",
    )

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: Optional[str]) -> Optional[str]:
        """Validate sort order is asc or desc."""
        if v is not None and v not in ("asc", "desc"):
            raise ValueError("sort_order must be 'asc' or 'desc'")
        return v


class ProjectConfigInput(BaseModel):
    """Project/logs configuration for fetching data."""

    project_name: str = Field(
        ...,
        description="Name of the project to fetch logs from",
    )
    context: Optional[str] = Field(
        None,
        description="Static context to filter logs by",
    )
    filter_expr: Optional[str] = Field(
        None,
        description="Boolean expression to filter entries",
    )
    from_fields: Optional[str] = Field(
        None,
        description="Fields to include (ampersand-separated)",
    )
    exclude_fields: Optional[str] = Field(
        None,
        description="Fields to exclude (ampersand-separated)",
    )
    limit: Optional[int] = Field(
        1000,
        description="Maximum number of logs to fetch",
        ge=1,
        le=10000,
    )
    offset: Optional[int] = Field(
        None,
        description="Number of logs to skip",
        ge=0,
    )
    sorting: Optional[str] = Field(
        None,
        description="JSON-encoded sorting configuration",
    )


class CreateTableViewRequest(BaseModel):
    """Request to create a new table view."""

    table_config: Optional[TableConfigInput] = Field(
        None,
        description="Table display configuration (columns, sorting, etc.)",
    )
    project_config: ProjectConfigInput = Field(
        ...,
        description="Project and logs configuration",
    )
    title: Optional[str] = Field(
        None,
        description="Optional title for the table view",
    )


class UpdateTableViewRequest(BaseModel):
    """Request to update an existing table view."""

    title: Optional[str] = Field(
        None,
        description="New title for the table view",
    )
    table_config: Optional[TableConfigInput] = Field(
        None,
        description="New table configuration",
    )
    project_config: Optional[ProjectConfigInput] = Field(
        None,
        description="New project configuration",
    )


class DeleteTableViewsByProjectRequest(BaseModel):
    """Request to delete all table views for a project/context pair."""

    project_name: str = Field(
        ...,
        description="Name of the project",
    )
    context: Optional[str] = Field(
        None,
        description="Optional context to filter by (deletes all if not specified)",
    )


# =============================================================================
# Output Schemas
# =============================================================================


class TableViewMetadata(BaseModel):
    """Metadata about a table view."""

    token: str = Field(..., description="Unique table view token")
    title: Optional[str] = Field(None, description="Table view title")
    project_name: str = Field(..., description="Name of the project")
    created_at: datetime = Field(..., description="When the table view was created")
    updated_at: Optional[datetime] = Field(
        None,
        description="When the table view was last updated",
    )
    created_by: str = Field(..., description="User ID of the creator")


class UserMetadata(BaseModel):
    """User/organization context for the table view."""

    user_id: str = Field(..., description="User ID of the creator")
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID (null for personal table views)",
    )


class TableViewResponse(BaseModel):
    """Full table view response."""

    url: str = Field(..., description="Shareable URL to view the table")
    token: str = Field(..., description="Unique table view token")
    table_config: Dict[str, Any] = Field(..., description="Table configuration")
    project_config: Dict[str, Any] = Field(..., description="Project configuration")
    table_view_metadata: TableViewMetadata = Field(
        ...,
        description="Table view metadata",
    )
    user_metadata: UserMetadata = Field(..., description="User/org context")


class TableViewListItem(BaseModel):
    """Table view item for list responses (metadata only)."""

    token: str = Field(..., description="Unique table view token")
    title: Optional[str] = Field(None, description="Table view title")
    project_name: str = Field(..., description="Name of the project")
    created_at: datetime = Field(..., description="When the table view was created")
    updated_at: Optional[datetime] = Field(
        None,
        description="When the table view was last updated",
    )
    created_by: str = Field(..., description="User ID of the creator")
    url: str = Field(..., description="Shareable URL to view the table")


class TableViewListResponse(BaseModel):
    """Response for list table views endpoint."""

    table_views: List[TableViewListItem] = Field(..., description="List of table views")
    count: int = Field(..., description="Total count of table views")


# =============================================================================
# Admin Schemas
# =============================================================================


class AdminTableViewResponse(BaseModel):
    """Admin response for table view retrieval (includes user_metadata for API key lookup)."""

    user_id: str = Field(..., description="User ID of the creator")
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID (null for personal table views)",
    )
    config: Dict[str, Any] = Field(..., description="Table configuration")
    project_config: Dict[str, Any] = Field(..., description="Project configuration")
    metadata: TableViewMetadata = Field(..., description="Table view metadata")
