"""Schemas for shared spaces, memberships, and invitations."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SpaceMembershipStatus(str, Enum):
    """Membership creation outcomes returned by the member-add endpoint."""

    active = "active"
    pending_invitation = "pending_invitation"


class SpaceCreate(BaseModel):
    """Request body for creating a space."""

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    organization_id: Optional[int] = None


class SpaceUpdate(BaseModel):
    """Request body for updating mutable space fields."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None


class SpaceRead(BaseModel):
    """Full space representation returned by lifecycle endpoints."""

    space_id: int
    name: str
    description: Optional[str] = None
    organization_id: Optional[int] = None
    owner_user_id: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SpaceSummary(BaseModel):
    """Compact space representation for assistant membership lists."""

    space_id: int
    name: str
    description: Optional[str] = None
    organization_id: Optional[int] = None
    status: str

    model_config = {"from_attributes": True}


class SpaceMemberCreate(BaseModel):
    """Request body for adding an assistant to a space."""

    assistant_id: int


class SpaceMember(BaseModel):
    """Live assistant membership in a space."""

    assistant_id: int
    space_id: int
    user_id: str
    organization_id: Optional[int] = None
    added_by: str
    created_at: datetime


class SpaceMembershipResponse(BaseModel):
    """Result of adding an assistant to a space."""

    membership_status: SpaceMembershipStatus
    assistant_id: int
    space_id: int
    invite_id: Optional[int] = None
    expires_at: Optional[datetime] = None


class SpaceInviteCreate(BaseModel):
    """Request body for inviting an assistant owner to join a space."""

    assistant_id: int


class SpaceInviteRead(BaseModel):
    """Invitation lifecycle state for adding an assistant to a space."""

    invite_id: int
    space_id: int
    assistant_id: int
    invited_by: str
    invited_owner_id: str
    status: str
    created_at: datetime
    expires_at: datetime
    decided_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SpaceInviteDecision(BaseModel):
    """Response for invitation accept/decline transitions."""

    status: str
