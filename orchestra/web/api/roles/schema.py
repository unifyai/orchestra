"""Role and permission management schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class PermissionResponse(BaseModel):
    """Schema for permission response."""

    id: int
    name: str
    description: Optional[str] = None
    resource_type: str
    action: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleResponse(BaseModel):
    """Schema for role response."""

    id: int
    name: str
    description: Optional[str] = None
    organization_id: Optional[int] = None
    is_system_role: bool
    created_at: datetime
    permissions: List[PermissionResponse] = []

    model_config = {"from_attributes": True}


class RoleCreate(BaseModel):
    """Schema for creating a custom role."""

    name: str
    description: Optional[str] = None
    permission_ids: List[int] = []


class RoleUpdate(BaseModel):
    """Schema for updating a custom role."""

    name: Optional[str] = None
    description: Optional[str] = None


class RolePermissionAdd(BaseModel):
    """Schema for adding permissions to a role."""

    permission_ids: List[int]


class RolePermissionRemove(BaseModel):
    """Schema for removing permissions from a role."""

    permission_ids: List[int]
