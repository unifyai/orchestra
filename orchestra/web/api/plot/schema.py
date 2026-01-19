"""Pydantic schemas for Plot API."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from orchestra.web.api.plot.validation import (
    VALID_AGGREGATES,
    VALID_METRICS,
    VALID_PLOT_TYPES,
    VALID_SCALES,
    VALID_SORT_ORDER,
    validate_hex_color,
)

# =============================================================================
# Input Schemas
# =============================================================================


class PlotConfigInput(BaseModel):
    """Plot configuration input from user."""

    type: Optional[str] = Field(
        None,
        description="Plot type: scatter, bar, histogram, or line",
    )
    x_axis: str = Field(
        ...,
        description="Field name for the x-axis",
    )
    y_axis: Optional[str] = Field(
        None,
        description="Field name for the y-axis (not required for histogram)",
    )
    group_by: Optional[str] = Field(
        None,
        description="Field name to group data by",
    )
    aggregate: Optional[str] = Field(
        None,
        description="Aggregation function: sum, mean, count, min, max",
    )
    scale_x: Optional[str] = Field(
        "linear",
        description="X-axis scale: linear or log",
    )
    scale_y: Optional[str] = Field(
        "linear",
        description="Y-axis scale: linear or log",
    )
    metric: Optional[str] = Field(
        "mean",
        description="Metric for aggregation: mean, sum, count, min, max",
    )
    bin_count: Optional[int] = Field(
        10,
        description="Number of bins for histogram",
        ge=1,
        le=100,
    )
    show_regression: Optional[bool] = Field(
        False,
        description="Show regression line (scatter plots)",
    )
    colors: Optional[Dict[str, str]] = Field(
        None,
        description="Custom colors for groups: {group_value: hex_color}",
    )
    sort_order: Optional[str] = Field(
        None,
        description="Sort order: unsorted, asc, or desc",
    )
    title: Optional[str] = Field(
        None,
        description="Title for the plot (can be inferred by LLM)",
    )
    x_label: Optional[str] = Field(
        None,
        description="Custom label for x-axis and tooltip (overrides field name)",
    )
    y_label: Optional[str] = Field(
        None,
        description="Custom label for y-axis and tooltip (overrides field name)",
    )
    # Axis visibility options
    show_x_label: Optional[bool] = Field(
        True,
        description="Whether to show the x-axis label",
    )
    show_y_label: Optional[bool] = Field(
        True,
        description="Whether to show the y-axis label",
    )
    # Tick formatting
    x_tick_format: Optional[str] = Field(
        None,
        description="Format string for x-axis ticks (e.g., '$' prefix for currency)",
    )
    y_tick_format: Optional[str] = Field(
        None,
        description="Format string for y-axis ticks (e.g., '$' prefix for currency)",
    )
    # Group by and aggregate labels
    group_by_label: Optional[str] = Field(
        None,
        description="Custom label for group_by field in tooltip and legend (overrides field name)",
    )
    aggregate_label: Optional[str] = Field(
        None,
        description="Custom label for aggregate field in tooltip (overrides field name)",
    )

    # =========================================================================
    # Validators
    # =========================================================================

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: Optional[str]) -> Optional[str]:
        """Validate plot type is one of the allowed values."""
        if v is not None and v not in VALID_PLOT_TYPES:
            raise ValueError(
                f"Invalid plot type '{v}'. Must be one of: {', '.join(VALID_PLOT_TYPES)}",
            )
        return v

    @field_validator("scale_x", "scale_y")
    @classmethod
    def validate_scale(cls, v: Optional[str]) -> Optional[str]:
        """Validate scale is linear or log."""
        if v is not None and v not in VALID_SCALES:
            raise ValueError(
                f"Invalid scale '{v}'. Must be one of: {', '.join(VALID_SCALES)}",
            )
        return v

    @field_validator("aggregate")
    @classmethod
    def validate_aggregate(cls, v: Optional[str]) -> Optional[str]:
        """Validate aggregate function is one of the allowed values."""
        if v is not None and v not in VALID_AGGREGATES:
            raise ValueError(
                f"Invalid aggregate '{v}'. Must be one of: {', '.join(VALID_AGGREGATES)}",
            )
        return v

    @field_validator("metric")
    @classmethod
    def validate_metric(cls, v: Optional[str]) -> Optional[str]:
        """Validate metric is one of the allowed values."""
        if v is not None and v not in VALID_METRICS:
            raise ValueError(
                f"Invalid metric '{v}'. Must be one of: {', '.join(VALID_METRICS)}",
            )
        return v

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: Optional[str]) -> Optional[str]:
        """Validate sort order is one of the allowed values."""
        if v is not None and v not in VALID_SORT_ORDER:
            raise ValueError(
                f"Invalid sort_order '{v}'. Must be one of: {', '.join(VALID_SORT_ORDER)}",
            )
        return v

    @field_validator("colors")
    @classmethod
    def validate_colors(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        """Validate all color values are valid hex colors."""
        if v is not None:
            invalid_colors = []
            for key, color in v.items():
                if not validate_hex_color(color):
                    invalid_colors.append(f"{key}: {color}")
            if invalid_colors:
                raise ValueError(
                    f"Invalid hex color(s): {', '.join(invalid_colors)}. "
                    "Colors must be in #RGB or #RRGGBB format.",
                )
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
    column_context: Optional[str] = Field(
        None,
        description="Column context for field resolution",
    )
    filter_expr: Optional[str] = Field(
        None,
        description="Boolean expression to filter entries",
    )
    from_ids: Optional[str] = Field(
        None,
        description="Log IDs to include (ampersand-separated)",
    )
    exclude_ids: Optional[str] = Field(
        None,
        description="Log IDs to exclude (ampersand-separated)",
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
    group_by: Optional[List[str]] = Field(
        None,
        description="Fields to group results by",
    )
    group_limit: Optional[int] = Field(
        None,
        description="Maximum number of groups at each level",
        ge=1,
    )
    group_offset: Optional[int] = Field(
        None,
        description="Number of groups to skip",
        ge=0,
    )
    group_depth: Optional[int] = Field(
        None,
        description="Maximum depth of nested groups",
    )
    groups_only: Optional[bool] = Field(
        None,
        description="Return only groups without full logs",
    )
    nested_groups: Optional[bool] = Field(
        None,
        description="Return groups as nested structure",
    )
    sorting: Optional[str] = Field(
        None,
        description="JSON-encoded sorting configuration",
    )
    group_sorting: Optional[str] = Field(
        None,
        description="JSON-encoded group sorting configuration",
    )
    value_limit: Optional[int] = Field(
        None,
        description="Maximum characters for string values",
    )
    randomize: Optional[bool] = Field(
        None,
        description="Return logs in random order",
    )
    seed: Optional[str] = Field(
        None,
        description="Seed for random ordering",
    )


class CreatePlotRequest(BaseModel):
    """Request to create a new plot."""

    plot_config: Optional[PlotConfigInput] = Field(
        None,
        description="Direct plot configuration",
    )
    description: Optional[str] = Field(
        None,
        description="Natural language description for LLM inference",
    )
    project_config: ProjectConfigInput = Field(
        ...,
        description="Project and logs configuration",
    )
    title: Optional[str] = Field(
        None,
        description="Optional title for the plot",
    )


class UpdatePlotRequest(BaseModel):
    """Request to update an existing plot."""

    title: Optional[str] = Field(
        None,
        description="New title for the plot",
    )
    plot_config: Optional[PlotConfigInput] = Field(
        None,
        description="New plot configuration",
    )
    project_config: Optional[ProjectConfigInput] = Field(
        None,
        description="New project configuration",
    )


class DeletePlotsByProjectRequest(BaseModel):
    """Request to delete all plots for a project/context pair."""

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


class PlotMetadata(BaseModel):
    """Metadata about a plot."""

    token: str = Field(..., description="Unique plot token")
    title: Optional[str] = Field(None, description="Plot title")
    project_name: str = Field(..., description="Name of the project")
    created_at: datetime = Field(..., description="When the plot was created")
    updated_at: Optional[datetime] = Field(None, description="When the plot was last updated")
    created_by: str = Field(..., description="User ID of the creator")


class UserMetadata(BaseModel):
    """User/organization context for the plot."""

    user_id: str = Field(..., description="User ID of the plot creator")
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID (null for personal plots)",
    )


class InferredConfigResponse(BaseModel):
    """Response for LLM-inferred configuration."""

    type: str = Field(..., description="Inferred plot type")
    x_axis: str = Field(..., description="Inferred x-axis field")
    y_axis: Optional[str] = Field(None, description="Inferred y-axis field")
    group_by: Optional[str] = Field(None, description="Inferred group by field")
    aggregate: Optional[str] = Field(None, description="Inferred aggregation function")
    scale_x: Optional[str] = Field(None, description="Inferred x-axis scale")
    scale_y: Optional[str] = Field(None, description="Inferred y-axis scale")
    metric: Optional[str] = Field(None, description="Inferred metric")
    bin_count: Optional[int] = Field(None, description="Inferred bin count (histogram)")
    show_regression: Optional[bool] = Field(
        None,
        description="Inferred show regression (scatter)",
    )
    sort_order: Optional[str] = Field(None, description="Inferred sort order (bar)")
    title: Optional[str] = Field(None, description="LLM-suggested plot title")
    x_label: Optional[str] = Field(None, description="LLM-suggested x-axis label")
    y_label: Optional[str] = Field(None, description="LLM-suggested y-axis label")
    confidence: float = Field(..., description="Confidence score 0-1")
    reasoning: Optional[str] = Field(None, description="Explanation of inference")


class PlotResponse(BaseModel):
    """Full plot response."""

    url: str = Field(..., description="Shareable URL to view the plot")
    token: str = Field(..., description="Unique plot token")
    plot_config: Dict[str, Any] = Field(..., description="Plot configuration")
    project_config: Dict[str, Any] = Field(..., description="Project configuration")
    plot_metadata: PlotMetadata = Field(..., description="Plot metadata")
    user_metadata: UserMetadata = Field(..., description="User/org context")
    inferred_config: Optional[InferredConfigResponse] = Field(
        None,
        description="LLM-inferred configuration (if description was used)",
    )


class PlotListItem(BaseModel):
    """Plot item for list responses (metadata only)."""

    token: str = Field(..., description="Unique plot token")
    title: Optional[str] = Field(None, description="Plot title")
    project_name: str = Field(..., description="Name of the project")
    created_at: datetime = Field(..., description="When the plot was created")
    updated_at: Optional[datetime] = Field(None, description="When the plot was last updated")
    created_by: str = Field(..., description="User ID of the creator")
    url: str = Field(..., description="Shareable URL to view the plot")


class PlotListResponse(BaseModel):
    """Response for list plots endpoint."""

    plots: List[PlotListItem] = Field(..., description="List of plots")
    count: int = Field(..., description="Total count of plots")


# =============================================================================
# Admin Schemas
# =============================================================================


class AdminPlotResponse(BaseModel):
    """Admin response for plot retrieval (includes user_metadata for API key lookup)."""

    user_id: str = Field(..., description="User ID of the plot creator")
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID (null for personal plots)",
    )
    config: Dict[str, Any] = Field(..., description="Plot configuration")
    project_config: Dict[str, Any] = Field(..., description="Project configuration")
    metadata: PlotMetadata = Field(..., description="Plot metadata")
