"""Pydantic schemas for team management."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, model_validator

from orchestra.web.api.utils.safe_text import (
    OptionalSafeLabel,
    OptionalSafeText,
    SafeLabel,
)


class TeamCreate(BaseModel):
    """Schema for creating a team."""

    name: SafeLabel
    description: OptionalSafeText = None


class TeamUpdate(BaseModel):
    """Schema for updating a team."""

    name: OptionalSafeLabel = None
    description: OptionalSafeText = None


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
    members: Optional[List[str]] = None  # User IDs, included when listing teams
    is_org_wide_sharing: bool = False
    image: Optional[str] = None


class TeamWithMembersResponse(BaseModel):
    """Schema for team response with members list."""

    id: int
    name: str
    description: Optional[str]
    organization_id: int
    created_at: datetime
    members: List[str]  # User IDs
    is_org_wide_sharing: bool = False
    image: Optional[str] = None


class TeamSummary(BaseModel):
    """Compact team metadata for assistant runtime payloads."""

    team_id: int
    name: str
    description: Optional[str] = None


class TeamMembershipStatus(str, Enum):
    """Membership creation outcomes returned by assistant-member endpoints."""

    active = "active"


class TeamAssistantMemberCreate(BaseModel):
    """Request body for adding one assistant to a team."""

    assistant_id: Optional[int] = None
    member_user_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_exactly_one_target(self) -> "TeamAssistantMemberCreate":
        if (self.assistant_id is None) == (self.member_user_id is None):
            raise ValueError(
                "exactly one of assistant_id or member_user_id is required",
            )
        return self


class TeamAssistantMember(BaseModel):
    """Assistant membership row exposed by team member endpoints."""

    assistant_id: int
    team_id: int
    user_id: str
    organization_id: Optional[int]
    added_by: str
    created_at: datetime


class TeamMembershipResponse(BaseModel):
    """Response for assistant membership mutations."""

    membership_status: TeamMembershipStatus
    assistant_id: int
    team_id: int


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


class UserResourceAccessEntry(BaseModel):
    """Schema for a single user access entry with source info."""

    id: int
    role_id: int
    role_name: str
    permissions: List[str]  # Permission names for this role, e.g., ["project:read"]
    grantee_type: str  # 'user' or 'team'
    source: str  # 'direct' or 'team'
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    created_at: datetime


class UserResourceAccessResponse(BaseModel):
    """Schema for a user's access on a specific resource."""

    user_id: str
    resource_type: str
    resource_id: int
    access_entries: List[UserResourceAccessEntry]
    effective_role: Optional[str] = None  # Highest permission level
