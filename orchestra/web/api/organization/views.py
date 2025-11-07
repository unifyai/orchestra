"""Organization management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.organization.schema import (
    OrganizationCreate,
    OrganizationMemberAdd,
    OrganizationResponse,
    OrganizationUpdate,
)
from orchestra.web.api.users.views import generate_key

router = APIRouter()


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    request_fastapi: Request,
    organization: OrganizationCreate,
    session: Session = Depends(get_db_session),
) -> OrganizationResponse:
    """
    Create a new organization.

    The authenticated user will be the owner of the organization.
    If billing_user_id is not provided, it defaults to the owner.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Check if organization name already exists
    existing = org_dao.filter(name=organization.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization with name '{organization.name}' already exists",
        )

    # Create organization
    try:
        org = org_dao.create(
            name=organization.name,
            owner_id=user_id,
            billing_user_id=organization.billing_user_id or user_id,
        )

        # Add creator as owner member
        org_member_dao.create(
            organization_id=org.id,
            user_id=user_id,
            level="owner",
        )

        session.commit()
        return OrganizationResponse.model_validate(org)
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

    # Check if user has access (is owner or member)
    is_owner = org.owner_id == user_id
    is_member = bool(
        org_member_dao.filter(
            organization_id=organization_id,
            user_id=user_id,
        ),
    )

    if not (is_owner or is_member):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this organization",
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

    Only the organization owner can update it.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can update it",
        )

    # Check for name conflict if name is being updated
    if organization.name and organization.name != org.name:
        existing = org_dao.filter(name=organization.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Organization with name '{organization.name}' already exists",
            )

    # Update organization
    try:
        org_dao.update(
            id=organization_id,
            name=organization.name,
            billing_user_id=organization.billing_user_id,
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

    Only the organization owner can delete it.
    This will also delete all associated data (projects, members, etc.).
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can delete it",
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

    Only the organization owner can add members.
    Automatically creates an organization-specific API key for the new member.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can add members",
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

    # Add member
    try:
        org_member_dao.create(
            organization_id=organization_id,
            user_id=member_data.user_id,
            level=member_data.level,
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

    Only the organization owner can remove members.
    Automatically revokes all organization-specific API keys for the member.
    Personal API keys are NOT affected.
    """
    requesting_user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)

    # Get organization
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if requesting user is the owner
    if org.owner_id != requesting_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can remove members",
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
