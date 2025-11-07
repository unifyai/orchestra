"""Organization management schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class OrganizationCreate(BaseModel):
    """Schema for creating an organization."""

    name: str
    billing_user_id: Optional[str] = None


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization."""

    name: Optional[str] = None
    billing_user_id: Optional[str] = None


class OrganizationResponse(BaseModel):
    """Schema for organization response."""

    id: int
    name: str
    owner_id: str
    billing_user_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OrganizationMemberAdd(BaseModel):
    """Schema for adding a member to an organization."""

    user_id: str
    level: str = "user"  # owner, admin, user


class OrganizationMemberRemove(BaseModel):
    """Schema for removing a member from an organization."""

    user_id: str
