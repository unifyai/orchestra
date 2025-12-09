"""Organization management schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class OrganizationCreate(BaseModel):
    """Schema for creating an organization."""

    name: str
    # Note: billing_user_id is always set to owner_id automatically


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization."""

    name: Optional[str] = None
    # Note: billing_user_id cannot be updated directly.
    # Use the transfer-ownership endpoint to change both owner and billing user.


class OrganizationOwnershipTransfer(BaseModel):
    """Schema for transferring organization ownership."""

    new_owner_id: str


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
    role_id: Optional[int] = None  # RBAC role (defaults to Member role if not provided)


class OrganizationMemberRemove(BaseModel):
    """Schema for removing a member from an organization."""

    user_id: str


class OrganizationMemberRoleUpdate(BaseModel):
    """Schema for updating a member's role."""

    role_id: int


class OrganizationMemberResponse(BaseModel):
    """Schema for organization member response."""

    id: int
    user_id: str
    organization_id: int
    level: str
    role_id: Optional[int]
    role_name: Optional[str] = None
    created_at: datetime
    # User info fields (populated from AuthUser)
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None

    model_config = {"from_attributes": True}


# ============== Organization Invite Schemas ==============


class InviteUserRequest(BaseModel):
    """Schema for inviting a user to an organization."""

    email: str
    role_id: Optional[int] = None  # Defaults to Member role if not provided
    level: str = "user"  # owner, admin, user
    expires_in_days: int = 7  # Default 7 days


class InviteResponse(BaseModel):
    """Schema for organization invite response.

    All invites in the system are pending - they are deleted when accepted/declined.
    """

    id: str
    token: str
    organization_id: int
    organization_name: str
    invitee_email: str
    invited_by_user_id: str
    invited_by_name: Optional[str] = None
    role_id: int
    role_name: Optional[str] = None
    level: str
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class InviteListResponse(BaseModel):
    """Schema for listing organization invites."""

    invites: list[InviteResponse]


class AcceptInviteResponse(BaseModel):
    """Schema for accepting an invite."""

    message: str
    organization_id: int
    organization_name: str
    api_key: str


class DeclineInviteResponse(BaseModel):
    """Schema for declining an invite."""

    message: str
