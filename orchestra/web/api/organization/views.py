"""Organization management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.organization.schema import (
    OrganizationCreate,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberRoleUpdate,
    OrganizationOwnershipTransfer,
    OrganizationResponse,
    OrganizationUpdate,
)
from orchestra.web.api.users.views import generate_key

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
            level="owner",
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

    # Check if user has org:read permission
    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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

    # Check if user has org:write permission
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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

    # Check if user has org:delete permission
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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

    # Check if user has org:write permission
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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

    # Block Owner role assignment via add_member
    if member_data.role_id:
        requested_role = role_dao.get(member_data.role_id)
        if requested_role and requested_role.name == "Owner":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign Owner role via add member. "
                "Use ownership transfer instead.",
            )

    # Add member
    try:
        # DAO will default to Member role if role_id is None
        org_member_dao.create(
            organization_id=organization_id,
            user_id=member_data.user_id,
            level=member_data.level,
            role_id=member_data.role_id,
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

    # Check if requesting user has org:write permission
    has_permission = resource_access_dao.check_user_permission(
        requesting_user_id,
        "org",
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

    # Check if user has org:read permission
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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
                level=member.level,
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

    # Check if user has org:write permission
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
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
            level=updated_member.level,
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

        # Update old owner's level to admin
        old_owner_member = org_member_dao.get_member(user_id, organization_id)
        if old_owner_member:
            org_member_dao.update(
                id=old_owner_member.id,
                level="admin",
            )

        # Update new owner's level to owner
        org_member_dao.update(
            id=new_owner_member.id,
            level="owner",
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
