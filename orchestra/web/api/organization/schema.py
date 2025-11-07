"""Schema for organization endpoints."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class OrganizationCreate(BaseModel):
    """Request model for creating an organization."""

    name: str = Field(..., description="Organization name", example="Acme Corp")
    billing_user_id: Optional[str] = Field(
        None,
        description="User ID for billing. Defaults to the creator if not specified.",
        example="user_123",
    )


class OrganizationUpdate(BaseModel):
    """Request model for updating an organization."""

    name: Optional[str] = Field(
        None,
        description="New organization name",
        example="Acme Inc",
    )
    billing_user_id: Optional[str] = Field(
        None,
        description="New billing user ID",
        example="user_456",
    )


class OrganizationResponse(BaseModel):
    """Response model for organization."""

    id: int = Field(..., description="Organization ID")
    name: str = Field(..., description="Organization name")
    owner_id: str = Field(..., description="Owner user ID")
    billing_user_id: str = Field(..., description="Billing user ID")
    created_at: datetime = Field(..., description="Creation timestamp")

    class Config:
        from_attributes = True
