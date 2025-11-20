"""Pydantic schemas for team management."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class TeamCreate(BaseModel):
    """Schema for creating a team."""

    name: str
    description: Optional[str] = None


class TeamUpdate(BaseModel):
    """Schema for updating a team."""

    name: Optional[str] = None
    description: Optional[str] = None


class TeamMemberAdd(BaseModel):
    """Schema for adding members to a team."""

    user_ids: List[str]


class TeamResponse(BaseModel):
    """Schema for team response."""

    id: int
    name: str
    description: Optional[str]
    organization_id: int
    created_at: datetime
    member_count: Optional[int] = None  # Populated when needed


class TeamWithMembersResponse(BaseModel):
    """Schema for team response with members list."""

    id: int
    name: str
    description: Optional[str]
    organization_id: int
    created_at: datetime
    members: List[str]  # User IDs


class ResourceAccessGrant(BaseModel):
    """Schema for granting resource access."""

    role_id: int
    grantee_type: str  # 'user' or 'team'
    grantee_id: str  # user_id or team_id


class ResourceAccessRevoke(BaseModel):
    """Schema for revoking resource access."""

    grantee_type: str  # 'user' or 'team'
    grantee_id: str  # user_id or team_id
    role_id: Optional[int] = None  # If None, revoke all access


class ResourceAccessUpdate(BaseModel):
    """Schema for updating resource access (changing role)."""

    role_id: int  # New role ID to assign


class ResourceAccessResponse(BaseModel):
    """Schema for resource access response."""

    id: int
    resource_type: str
    resource_id: int
    role_id: int
    role_name: str
    grantee_type: str
    grantee_id: str
    grantee_name: Optional[str] = None  # User email or team name
    created_at: datetime


class ResourceAccessListResponse(BaseModel):
    """Schema for listing resource access."""

    resource_type: str
    resource_id: int
    access_entries: List[ResourceAccessResponse]
