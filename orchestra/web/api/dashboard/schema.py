"""Pydantic schemas for dashboard token endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class RegisterTokenRequest(BaseModel):
    """Request body for POST /dashboards/tokens."""

    token: str = Field(..., min_length=1, max_length=12)
    entity_type: str = Field(..., pattern="^(tile|dashboard)$")
    context_name: str = Field(..., min_length=1, max_length=500)
    project_name: str = Field(..., min_length=1, max_length=200)


class RegisterTokenResponse(BaseModel):
    """Response for successful token registration."""

    token: str
    entity_type: str
    context_name: str


class TokenResolutionResponse(BaseModel):
    """Response for admin token resolution."""

    entity_type: str
    context_name: str
    user_id: str
    organization_id: Optional[int] = None
    project_id: int


class DataBridgeRequest(BaseModel):
    """Request body for the data bridge proxy.

    Field names match GET /v0/logs query params so the endpoint
    can forward them without translation.
    """

    context: str
    filter_expr: Optional[str] = None
    from_fields: Optional[str] = None
    exclude_fields: Optional[str] = None
    sorting: Optional[str] = None
    limit: Optional[int] = Field(None, ge=1, le=1000)
    offset: Optional[int] = Field(None, ge=0)
    column_context: Optional[str] = None
    group_by: Optional[list[str]] = None
    randomize: Optional[bool] = None


class DataBridgeResponse(BaseModel):
    """Response from the data bridge proxy.

    Rows are flattened log entries (entries + derived_entries merged)
    for ergonomic JS consumption.
    """

    rows: list[dict]
    total_count: int
