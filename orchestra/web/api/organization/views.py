"""Organization management endpoints."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.organization.schema import (
    AcceptInviteResponse,
    DeclineInviteResponse,
    InviteListResponse,
    InviteResponse,
    InviteUserRequest,
    OrganizationBillingResponse,
    OrganizationBillingUpdate,
    OrganizationBusinessProfileResponse,
    OrganizationBusinessProfileUpdate,
    OrganizationCreate,
    OrganizationCreditsResponse,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberRoleUpdate,
    OrganizationOwnershipTransfer,
    OrganizationResponse,
    OrganizationUpdate,
)
from orchestra.web.api.users.views import generate_key
from orchestra.web.api.utils.email import send_email_async

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/organizations",
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    request_fastapi: Request,
    organization: OrganizationCreate,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Create a new organization.

    The authenticated user will be the owner of the organization.
    billing_user_id is always set to the owner (billing follows ownership).
    Returns the organization details and the owner's organization API key.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)
    role_dao = RoleDAO(session)

    # Check if organization name already exists
    existing = org_dao.filter(name=organization.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization with name '{organization.name}' already exists",
        )

    # Create organization
    try:
        # billing_user_id always equals owner_id
        org = org_dao.create(
            name=organization.name,
            owner_id=user_id,
            billing_user_id=user_id,
        )

        # Get Owner system role
        owner_role = role_dao.get_by_name("Owner", organization_id=None)
        if not owner_role:
            raise ValueError("Owner system role not found")

        # Add creator as owner member with Owner role
        org_member_dao.create(
            organization_id=org.id,
            user_id=user_id,
            role_id=owner_role.id,
        )

        # Create organization API key for the owner
        new_api_key = generate_key()
        api_key_dao.create(
            key=new_api_key,
            name=f"org_{org.name}",
            user_id=user_id,
            organization_id=org.id,
        )

        session.commit()

        org_response = OrganizationResponse.model_validate(org)
        return {
            **org_response.model_dump(),
            "api_key": new_api_key,
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create organization: {str(e)}",
        )


@router.get("/organizations", response_model=List[OrganizationResponse])
async def list_organizations(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> List[OrganizationResponse]:
    """
    List all organizations the authenticated user has access to.

    This includes organizations where the user is:
    - The owner
    - A member
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)

    organizations = org_dao.get_user_organizations(user_id)

    return [OrganizationResponse.model_validate(org) for org in organizations]


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> OrganizationResponse:
    """Get details of a specific organization."""
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:read permission via org membership role
    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this organization",
        )

    return OrganizationResponse.model_validate(org)


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    request_fastapi: Request,
    organization_id: int,
    organization: OrganizationUpdate,
    session: Session = Depends(get_db_session),
) -> OrganizationResponse:
    """
    Update an organization.

    Requires org:write permission.
    Note: To change owner or billing_user_id, use the transfer-ownership endpoint.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update this organization",
        )

    # Check for name conflict if name is being updated
    if organization.name and organization.name != org.name:
        existing = org_dao.filter(name=organization.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Organization with name '{organization.name}' already exists",
            )

    # Update organization (only name can be updated here)
    try:
        org_dao.update(
            id=organization_id,
            name=organization.name,
        )
        session.commit()

        # Refresh to get updated data
        updated_org = org_dao.get(organization_id)
        return OrganizationResponse.model_validate(updated_org)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update organization: {str(e)}",
        )


@router.delete(
    "/organizations/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_organization(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Delete an organization.

    Requires org:delete permission (typically only Owner role has this).
    This will also delete all associated data (projects, members, etc.).
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:delete permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:delete",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this organization",
        )

    # Delete organization
    try:
        org_dao.delete(organization_id)
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete organization: {str(e)}",
        )


@router.post(
    "/organizations/{organization_id}/members",
    status_code=status.HTTP_201_CREATED,
)
async def add_organization_member(
    request_fastapi: Request,
    organization_id: int,
    member_data: OrganizationMemberAdd,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Add a member to an organization.

    Requires org:write permission.
    Automatically creates an organization-specific API key for the new member.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)
    role_dao = RoleDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to add members to this organization",
        )

    # Check if member already exists
    existing_member = org_member_dao.filter(
        organization_id=organization_id,
        user_id=member_data.user_id,
    )
    if existing_member:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this organization",
        )

    # Determine role_id - default to Member role if not provided
    role_id = member_data.role_id
    if role_id is None:
        member_role = role_dao.get_by_name("Member", organization_id=None)
        if not member_role:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Member system role not found",
            )
        role_id = member_role.id
    else:
        # Block Owner role assignment via add_member
        requested_role = role_dao.get(role_id)
        if requested_role and requested_role.name == "Owner":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign Owner role via add member. "
                "Use ownership transfer instead.",
            )

    # Add member
    try:
        org_member_dao.create(
            organization_id=organization_id,
            user_id=member_data.user_id,
            role_id=role_id,
        )

        # Create organization API key for the new member
        new_api_key = generate_key()
        api_key_dao.create(
            key=new_api_key,
            name=f"org_{org.name}",
            user_id=member_data.user_id,
            organization_id=organization_id,
        )

        session.commit()

        return {
            "message": "Member added successfully",
            "user_id": member_data.user_id,
            "api_key": new_api_key,
        }
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add member: {str(e)}",
        )


@router.delete(
    "/organizations/{organization_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_organization_member(
    request_fastapi: Request,
    organization_id: int,
    user_id: str,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Remove a member from an organization.

    Requires org:write permission.
    Automatically revokes all organization-specific API keys for the member.
    Personal API keys are NOT affected.
    """
    requesting_user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if requesting user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        requesting_user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to remove members from this organization",
        )

    # Don't allow removing the owner
    if user_id == org.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the organization owner",
        )

    # Check if member exists
    existing_member = org_member_dao.filter(
        organization_id=organization_id,
        user_id=user_id,
    )
    if not existing_member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this organization",
        )

    # Remove member and revoke organization API keys
    try:
        # Revoke organization API keys (personal keys are NOT affected)
        revoked_count = api_key_dao.revoke_organization_keys(
            user_id=user_id,
            organization_id=organization_id,
        )

        # Remove member from organization
        member = existing_member[0][0]
        org_member_dao.delete(member.id)

        session.commit()
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove member: {str(e)}",
        )


@router.get(
    "/organizations/{organization_id}/members",
    response_model=List[OrganizationMemberResponse],
)
async def list_organization_members(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> List[OrganizationMemberResponse]:
    """
    List all members of an organization with their roles.

    Requires org:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)
    resource_access_dao = ResourceAccessDAO(session)
    auth_user_dao = AuthUserDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:read permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view members of this organization",
        )

    # Get all members
    all_members_result = org_member_dao.filter(organization_id=organization_id)

    # Build response with role names and user info
    members_response = []
    for member_row in all_members_result:
        member = member_row[0]
        role_name = None
        if member.role_id:
            role = role_dao.get(member.role_id)
            role_name = role.name if role else None

        # Fetch user info
        user_info_row = auth_user_dao.get_by_id(member.user_id)
        user_name = None
        user_email = None
        user_image = None
        if user_info_row:
            # get_by_id returns a Row, extract the AuthUser model
            user_info = user_info_row[0]
            # Combine first and last name if available
            name_parts = []
            if user_info.name:
                name_parts.append(user_info.name)
            if user_info.last_name:
                name_parts.append(user_info.last_name)
            user_name = " ".join(name_parts) if name_parts else None
            user_email = user_info.email
            user_image = user_info.image

        members_response.append(
            OrganizationMemberResponse(
                id=member.id,
                user_id=member.user_id,
                organization_id=member.organization_id,
                role_id=member.role_id,
                role_name=role_name,
                created_at=member.created_at,
                name=user_name,
                email=user_email,
                image=user_image,
            ),
        )

    return members_response


@router.patch(
    "/organizations/{organization_id}/members/{member_user_id}/role",
    response_model=OrganizationMemberResponse,
)
async def update_member_role(
    request_fastapi: Request,
    organization_id: int,
    member_user_id: str,
    role_update: OrganizationMemberRoleUpdate,
    session: Session = Depends(get_db_session),
) -> OrganizationMemberResponse:
    """
    Update an organization member's RBAC role.

    Requires org:write permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update member roles in this organization",
        )

    # Cannot change the owner's role
    if member_user_id == org.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change the organization owner's role",
        )

    # Verify the role exists and is a system role
    role = role_dao.get(role_update.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_update.role_id} not found",
        )

    if not role.is_system_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only system roles can be assigned to members",
        )

    # Block Owner role assignment via update_member_role
    if role.name == "Owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign Owner role. Use ownership transfer instead.",
        )

    # Get the member
    member = org_member_dao.get_member(member_user_id, organization_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this organization",
        )

    # Update role
    try:
        org_member_dao.update_member_role(
            user_id=member_user_id,
            organization_id=organization_id,
            role_id=role_update.role_id,
        )
        session.commit()

        # Return updated member
        updated_member = org_member_dao.get_member(member_user_id, organization_id)
        return OrganizationMemberResponse(
            id=updated_member.id,
            user_id=updated_member.user_id,
            organization_id=updated_member.organization_id,
            role_id=updated_member.role_id,
            role_name=role.name,
            created_at=updated_member.created_at,
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update member role: {str(e)}",
        )


@router.post(
    "/organizations/{organization_id}/transfer-ownership",
    response_model=OrganizationResponse,
)
async def transfer_organization_ownership(
    request_fastapi: Request,
    organization_id: int,
    transfer: OrganizationOwnershipTransfer,
    session: Session = Depends(get_db_session),
) -> OrganizationResponse:
    """
    Transfer organization ownership to another member.

    Only the current owner can transfer ownership.
    The new owner must already be a member of the organization.

    Changes applied:
    - org.owner_id → new_owner_id
    - org.billing_user_id → new_owner_id (billing always follows owner)
    - new_owner's role → Owner
    - old_owner's role → Admin
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Only current owner can transfer ownership
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can transfer ownership",
        )

    # Cannot transfer to self
    if transfer.new_owner_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot transfer ownership to yourself",
        )

    # New owner must be an existing member
    new_owner_member = org_member_dao.get_member(transfer.new_owner_id, organization_id)
    if not new_owner_member:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New owner must be an existing member of the organization",
        )

    try:
        # Get role IDs
        owner_role = role_dao.get_by_name("Owner", organization_id=None)
        admin_role = role_dao.get_by_name("Admin", organization_id=None)

        if not owner_role or not admin_role:
            raise ValueError("Required system roles not found")

        # Update organization: owner_id and billing_user_id
        org_dao.update(
            id=organization_id,
            owner_id=transfer.new_owner_id,
            billing_user_id=transfer.new_owner_id,
        )

        # Update new owner's role to Owner
        org_member_dao.update_member_role(
            user_id=transfer.new_owner_id,
            organization_id=organization_id,
            role_id=owner_role.id,
        )

        # Update old owner's role to Admin
        org_member_dao.update_member_role(
            user_id=user_id,
            organization_id=organization_id,
            role_id=admin_role.id,
        )

        session.commit()

        updated_org = org_dao.get(organization_id)
        return OrganizationResponse.model_validate(updated_org)

    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transfer ownership: {str(e)}",
        )


# ============== Organization Invite Endpoints ==============


def _build_invite_response(
    invite,
    org,
    role_dao: RoleDAO,
    auth_user_dao: AuthUserDAO,
) -> InviteResponse:
    """Helper to build InviteResponse from invite object."""
    role_name = None
    if invite.role_id:
        role = role_dao.get(invite.role_id)
        role_name = role.name if role else None

    invited_by_name = None
    inviter_row = auth_user_dao.get_by_id(invite.invited_by_user_id)
    if inviter_row:
        inviter = inviter_row[0]
        name_parts = []
        if inviter.name:
            name_parts.append(inviter.name)
        if inviter.last_name:
            name_parts.append(inviter.last_name)
        invited_by_name = " ".join(name_parts) if name_parts else inviter.email

    return InviteResponse(
        id=invite.id,
        token=invite.token,
        organization_id=invite.organization_id,
        organization_name=org.name,
        invitee_email=invite.invitee_email,
        invited_by_user_id=invite.invited_by_user_id,
        invited_by_name=invited_by_name,
        role_id=invite.role_id,
        role_name=role_name,
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


@router.post(
    "/organizations/{organization_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user_to_organization(
    request_fastapi: Request,
    organization_id: int,
    invite_request: InviteUserRequest,
    session: Session = Depends(get_db_session),
) -> InviteResponse:
    """
    Invite a user to join an organization via email.

    Requires org:write permission.
    Sends an email with an invite link to the specified email address.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    role_dao = RoleDAO(session)
    auth_user_dao = AuthUserDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to invite users to this organization",
        )

    email = invite_request.email.lower()

    # Check if user is already a member
    # First, try to find the user by email
    existing_user_row = auth_user_dao.filter(email=email)
    if existing_user_row:
        existing_user = existing_user_row[0][0]
        existing_member = org_member_dao.filter(
            organization_id=organization_id,
            user_id=existing_user.id,
        )
        if existing_member:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member of this organization",
            )

    # Check for existing invite
    existing_invite = invite_dao.get_by_email_and_org(email, organization_id)
    if existing_invite:
        # Refresh expiry and resend the email
        existing_invite.expires_at = datetime.now(timezone.utc) + timedelta(
            days=invite_request.expires_in_days,
        )
        session.commit()
        await _send_invite_email(existing_invite, org, auth_user_dao, user_id)
        return _build_invite_response(existing_invite, org, role_dao, auth_user_dao)

    # Determine role_id (default to Member role)
    role_id = invite_request.role_id
    if not role_id:
        member_role = role_dao.get_by_name("Member", organization_id=None)
        if not member_role:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Default Member role not found",
            )
        role_id = member_role.id

    # Block Owner role assignment via invite
    requested_role = role_dao.get(role_id)
    if requested_role and requested_role.name == "Owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign Owner role via invite. Use ownership transfer instead.",
        )

    # Create invite
    invitee_user_id = None
    if existing_user_row:
        invitee_user_id = existing_user_row[0][0].id

    try:
        invite = invite_dao.create(
            organization_id=organization_id,
            invitee_email=email,
            invited_by_user_id=user_id,
            role_id=role_id,
            expires_in_days=invite_request.expires_in_days,
            invitee_user_id=invitee_user_id,
        )
        session.commit()

        # Send invite email
        await _send_invite_email(invite, org, auth_user_dao, user_id)

        return _build_invite_response(invite, org, role_dao, auth_user_dao)

    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create invite: {str(e)}",
        )


async def _send_invite_email(
    invite,
    org,
    auth_user_dao: AuthUserDAO,
    inviter_user_id: str,
) -> None:
    """Send the invitation email."""
    # Get inviter info
    inviter_name = "A team member"
    inviter_row = auth_user_dao.get_by_id(inviter_user_id)
    if inviter_row:
        inviter = inviter_row[0]
        if inviter.name:
            inviter_name = inviter.name
            if inviter.last_name:
                inviter_name += f" {inviter.last_name}"

    # Build invite link
    frontend_url = os.getenv(
        "UNIFY_CONSOLE_FRONTEND_URL", "https://console.unify.ai"
    ).rstrip("/")
    invite_link = f"{frontend_url}/invite?token={invite.token}"

    email_subject = f"You've been invited to join {org.name}"
    email_body = f"""
    <html>
    <body>
        <h2>You've been invited to join {org.name}</h2>
        <p>{inviter_name} has invited you to join the <strong>{org.name}</strong> organization on Unify.</p>
        <p>Click the link below to accept the invitation:</p>
        <p><a href="{invite_link}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Accept Invitation</a></p>
        <p>Or copy and paste this link into your browser:</p>
        <p>{invite_link}</p>
        <p>This invitation expires on {invite.expires_at.strftime('%B %d, %Y at %H:%M UTC')}.</p>
        <p>If you don't have a Unify account yet, you'll be able to create one after clicking the link.</p>
    </body>
    </html>
    """

    try:
        email_task = asyncio.create_task(
            send_email_async(invite.invitee_email, email_subject, email_body),
        )

        def _log_email_result(task: asyncio.Task) -> None:
            try:
                task.result()
                logger.info(f"Invite email sent to {invite.invitee_email}")
            except Exception as e:
                logger.error(
                    f"Failed to send invite email to {invite.invitee_email}: {e}",
                )

        email_task.add_done_callback(_log_email_result)
    except Exception as e:
        logger.error(f"Failed to schedule invite email: {e}")


@router.get(
    "/organizations/{organization_id}/invites",
    response_model=InviteListResponse,
)
async def list_organization_invites(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> InviteListResponse:
    """
    List pending invites for an organization.

    Requires org:read permission.
    All invites returned are pending (not expired).
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    role_dao = RoleDAO(session)
    auth_user_dao = AuthUserDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view invites for this organization",
        )

    invites = invite_dao.list_by_organization(organization_id)

    return InviteListResponse(
        invites=[
            _build_invite_response(invite, org, role_dao, auth_user_dao)
            for invite in invites
        ],
    )


@router.delete(
    "/organizations/{organization_id}/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_organization_invite(
    request_fastapi: Request,
    organization_id: int,
    invite_id: str,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Cancel a pending invite.

    Requires org:write permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to cancel invites for this organization",
        )

    # Get and verify invite
    invite = invite_dao.get_by_id(invite_id)
    if not invite or invite.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found",
        )

    invite_dao.delete(invite_id)
    session.commit()


@router.get(
    "/invites/pending",
    response_model=InviteListResponse,
)
async def list_my_pending_invites(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> InviteListResponse:
    """
    List all pending invites for the current user's email.
    """
    user_id = request_fastapi.state.user_id
    auth_user_dao = AuthUserDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    org_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    # Get current user's email
    user_row = auth_user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user = user_row[0]

    invites = invite_dao.list_by_email(user.email)

    invite_responses = []
    for invite in invites:
        org = org_dao.get(invite.organization_id)
        if org:
            invite_responses.append(
                _build_invite_response(invite, org, role_dao, auth_user_dao),
            )

    return InviteListResponse(invites=invite_responses)


@router.post(
    "/invites/{token}/accept",
    response_model=AcceptInviteResponse,
)
async def accept_invite(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> AcceptInviteResponse:
    """
    Accept an organization invite.

    The invite must be pending and not expired.
    The current user's email must match the invite email.
    """
    user_id = request_fastapi.state.user_id
    auth_user_dao = AuthUserDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)

    # Get current user
    user_row = auth_user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user = user_row[0]

    # Get invite by token
    invite = invite_dao.get_by_token(token)
    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found or invalid token",
        )

    # Verify user's email matches invite
    if user.email.lower() != invite.invitee_email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invite is for a different email address",
        )

    # Check if invite is valid
    is_valid, error_msg = invite_dao.is_valid_for_acceptance(invite)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # Get organization
    org = org_dao.get(invite.organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization no longer exists",
        )

    # Check if already a member
    existing_member = org_member_dao.filter(
        organization_id=invite.organization_id,
        user_id=user_id,
    )
    if existing_member:
        # Delete invite since user is already a member
        invite_dao.delete_invite(invite)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already a member of this organization",
        )

    try:
        # Add user as member
        org_member_dao.create(
            organization_id=invite.organization_id,
            user_id=user_id,
            role_id=invite.role_id,
        )

        # Create organization API key
        new_api_key = generate_key()
        api_key_dao.create(
            key=new_api_key,
            name=f"org_{org.name}",
            user_id=user_id,
            organization_id=invite.organization_id,
        )

        # Delete the invite (accepted)
        invite_dao.delete_invite(invite)

        session.commit()

        return AcceptInviteResponse(
            message="Successfully joined organization",
            organization_id=org.id,
            organization_name=org.name,
            api_key=new_api_key,
        )

    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to join organization: {str(e)}",
        )


@router.post(
    "/invites/{token}/decline",
    response_model=DeclineInviteResponse,
)
async def decline_invite(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> DeclineInviteResponse:
    """
    Decline an organization invite.
    """
    user_id = request_fastapi.state.user_id
    auth_user_dao = AuthUserDAO(session)
    invite_dao = OrganizationInviteDAO(session)

    # Get current user
    user_row = auth_user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user = user_row[0]

    # Get invite by token
    invite = invite_dao.get_by_token(token)
    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found or invalid token",
        )

    # Verify user's email matches invite
    if user.email.lower() != invite.invitee_email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invite is for a different email address",
        )

    # Delete the invite (declined)
    invite_dao.delete_invite(invite)
    session.commit()

    return DeclineInviteResponse(message="Invite declined")


# ============== Organization Billing Endpoints ==============


@router.get(
    "/organizations/{organization_id}/billing",
    tags=["organization-billing"],
)
async def get_organization_billing(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get billing information for an organization.

    Returns billing mode (delegated or direct), credits, and billing settings.
    Requires billing:read permission.
    """
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_billing_dao = OrganizationBillingDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view billing for this organization",
        )

    # Determine billing mode
    has_direct_billing = org_billing_dao.has_direct_billing(organization_id)
    billing_mode = "direct" if has_direct_billing else "delegated"

    # Get credits based on billing mode
    if has_direct_billing:
        credits = float(org_billing_dao.get_credits(organization_id))
    else:
        # For delegated billing, show 0 (credits are on the billing user's account)
        credits = 0.0

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        billing_mode=billing_mode,
        credits=credits,
        billing_user_id=org.billing_user_id if not has_direct_billing else None,
        stripe_customer_id=org.stripe_customer_id if has_direct_billing else None,
        autorecharge=org.autorecharge,
        autorecharge_threshold=float(org.autorecharge_threshold),
        autorecharge_qty=float(org.autorecharge_qty),
        account_status=org.account_status,
        billing_setup_complete=org.billing_setup_complete,
    ).model_dump()


@router.patch(
    "/organizations/{organization_id}/billing",
    tags=["organization-billing"],
)
async def update_organization_billing(
    request_fastapi: Request,
    organization_id: int,
    billing_update: "OrganizationBillingUpdate",
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Update billing settings for an organization.

    Requires billing:write permission.
    Owners and Admins have this permission by default.
    """
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_billing_dao = OrganizationBillingDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:write permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update billing settings",
        )

    # Update settings
    if billing_update.autorecharge is not None:
        org_billing_dao.set_autorecharge(organization_id, billing_update.autorecharge)

    if billing_update.autorecharge_threshold is not None:
        org_billing_dao.set_autorecharge_threshold(
            organization_id,
            billing_update.autorecharge_threshold,
        )

    if billing_update.autorecharge_qty is not None:
        org_billing_dao.set_autorecharge_qty(
            organization_id,
            billing_update.autorecharge_qty,
        )

    session.commit()

    # Return updated billing info
    has_direct_billing = org_billing_dao.has_direct_billing(organization_id)
    billing_mode = "direct" if has_direct_billing else "delegated"
    credits = (
        float(org_billing_dao.get_credits(organization_id))
        if has_direct_billing
        else 0.0
    )

    # Refresh org to get updated values
    session.refresh(org)

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        billing_mode=billing_mode,
        credits=credits,
        billing_user_id=org.billing_user_id if not has_direct_billing else None,
        stripe_customer_id=org.stripe_customer_id if has_direct_billing else None,
        autorecharge=org.autorecharge,
        autorecharge_threshold=float(org.autorecharge_threshold),
        autorecharge_qty=float(org.autorecharge_qty),
        account_status=org.account_status,
        billing_setup_complete=org.billing_setup_complete,
    ).model_dump()


@router.get(
    "/organizations/{organization_id}/billing/credits",
    tags=["organization-billing"],
)
async def get_organization_credits(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get credit balance for an organization.

    For direct billing orgs, returns the org's credit balance.
    For delegated billing orgs, returns the billing user's credit balance.
    Requires billing:read permission.
    """
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_billing_dao = OrganizationBillingDAO(session)
    users_dao = UsersDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view credits for this organization",
        )

    # Get credits based on billing mode
    if org_billing_dao.has_direct_billing(organization_id):
        credits = float(org_billing_dao.get_credits(organization_id))
    else:
        # Delegated billing - get from billing user
        if org.billing_user_id:
            billing_user = users_dao.get_user_with_id(org.billing_user_id)
            credits = float(billing_user.credits)
        else:
            credits = 0.0

    return OrganizationCreditsResponse(
        organization_id=organization_id,
        credits=credits,
    ).model_dump()


@router.get(
    "/organizations/{organization_id}/billing/business-profile",
    tags=["organization-billing"],
)
async def get_organization_business_profile(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get business profile for an organization (invoicing information).

    Requires billing:read permission.
    """
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_billing_dao = OrganizationBillingDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:read permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view business profile",
        )

    profile = org_billing_dao.get_business_profile(organization_id)
    return OrganizationBusinessProfileResponse(**profile).model_dump()


@router.patch(
    "/organizations/{organization_id}/billing/business-profile",
    tags=["organization-billing"],
)
async def update_organization_business_profile(
    request_fastapi: Request,
    organization_id: int,
    profile_update: "OrganizationBusinessProfileUpdate",
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Update business profile for an organization.

    Requires billing:write permission.
    Owners and Admins have this permission by default.
    """
    from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_billing_dao = OrganizationBillingDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Check billing:write permission
    has_permission = resource_access_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        "billing:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update business profile",
        )

    # Update profile
    billing_address_dict = None
    if profile_update.billing_address is not None:
        billing_address_dict = profile_update.billing_address.model_dump(
            exclude_none=True,
        )

    # Validate tax_id if provided along with country
    if profile_update.tax_id is not None:
        # Get country from billing_address (either new or existing)
        country = None
        if billing_address_dict and billing_address_dict.get("country"):
            country = billing_address_dict["country"]
        elif org.billing_address and org.billing_address.get("country"):
            country = org.billing_address["country"]

        if country:
            from orchestra.web.api.utils.tax_id_validator import TaxIDValidator

            is_valid, formatted_id, error = TaxIDValidator.validate_tax_id(
                profile_update.tax_id,
                country,
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid tax ID for {country}: {error}",
                )
            # Use the formatted version if validation succeeded
            profile_update.tax_id = formatted_id

    org_billing_dao.update_business_profile(
        organization_id,
        billing_email=profile_update.billing_email,
        business_name=profile_update.business_name,
        tax_id=profile_update.tax_id,
        billing_address=billing_address_dict,
    )
    session.commit()

    # Return updated profile
    profile = org_billing_dao.get_business_profile(organization_id)
    return OrganizationBusinessProfileResponse(**profile).model_dump()
