"""Role and permission management schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from orchestra.web.api.utils.safe_text import (
    OptionalSafeLabel,
    OptionalSafeText,
    SafeLabel,
)


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

    name: SafeLabel
    description: OptionalSafeText = None
    permission_ids: List[int] = []


class RoleUpdate(BaseModel):
    """Schema for updating a custom role."""

    name: OptionalSafeLabel = None
    description: OptionalSafeText = None


class RolePermissionAdd(BaseModel):
    """Schema for adding permissions to a role."""

    permission_ids: List[int]


class RolePermissionRemove(BaseModel):
    """Schema for removing permissions from a role."""

    permission_ids: List[int]
