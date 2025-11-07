"""Organization management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.organization.schema import (
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)

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
