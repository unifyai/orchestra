"""Organization management endpoints."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.billing_account_dao import (
    MIN_AUTORECHARGE_AMOUNT,
    BillingAccountDAO,
)
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.services.contact_sync_service import ContactSyncService
from orchestra.settings import settings
from orchestra.web.api.organization.schema import (
    AcceptInviteResponse,
    DeclineInviteResponse,
    InviteListResponse,
    InviteResponse,
    InviteUserRequest,
    MemberSpendingLimitRequest,
    MemberSpendingLimitResponse,
    MemberSpendResponse,
    OrganizationBillingResponse,
    OrganizationBillingUpdate,
    OrganizationBusinessProfileResponse,
    OrganizationBusinessProfileUpdate,
    OrganizationCheckoutRequest,
    OrganizationCreate,
    OrganizationCreditsResponse,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberRoleUpdate,
    OrganizationOwnershipTransfer,
    OrganizationResponse,
    OrganizationStripeCustomerCreateRequest,
    OrganizationUpdate,
    OrgSpendingLimitRequest,
    OrgSpendingLimitResponse,
    OrgSpendResponse,
)
from orchestra.web.api.users.views import generate_key
from orchestra.web.api.utils.email import send_email_async

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


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
    timezone is initialized from the owner's timezone setting.
    Returns the organization details and the owner's organization API key.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)
    role_dao = RoleDAO(session)
    user_dao = UserDAO(session)

    # Check if organization name already exists
    existing = org_dao.filter(name=organization.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization with name '{organization.name}' already exists",
        )

    # Determine timezone: use provided value, fall back to owner's, then UTC
    if organization.timezone is not None:
        org_timezone = organization.timezone
    else:
        owner_row = user_dao.get_by_id(user_id)
        org_timezone = owner_row[0].timezone if owner_row else None

    # Create organization
    try:
        # timezone: provided > owner's timezone > None (runtime defaults to UTC)
        org = org_dao.create(
            name=organization.name,
            owner_id=user_id,
            timezone=org_timezone,
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


@router.get(
    "/organizations/members",
    response_model=List[OrganizationMemberResponse],
)
async def list_organization_members_by_api_key(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> List[OrganizationMemberResponse]:
    """
    List all members of the organization associated with the API key.

    For org API key: Returns all members with their roles.
    For personal API key: Returns empty list.
    """
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Personal API key - return empty list
    if organization_id is None:
        return []

    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)
    resource_access_dao = ResourceAccessDAO(session)
    user_dao = UserDAO(session)

    # Verify organization exists
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
        user_info_row = user_dao.get_by_id(member.user_id)
        user_name = None
        user_email = None
        user_image = None
        user_bio = None
        user_timezone = None
        user_phone_number = None
        if user_info_row:
            user_info = user_info_row[0]
            name_parts = []
            if user_info.name:
                name_parts.append(user_info.name)
            if user_info.last_name:
                name_parts.append(user_info.last_name)
            user_name = " ".join(name_parts) if name_parts else None
            user_email = user_info.email
            user_image = user_info.image
            user_bio = user_info.bio
            user_timezone = user_info.timezone
            user_phone_number = user_info.phone_number

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
                bio=user_bio,
                timezone=user_timezone,
                phone_number=user_phone_number,
            ),
        )

    return members_response


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
    Note: To change owner, use the transfer-ownership endpoint.
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

    # Update organization (name and timezone can be updated here)
    try:
        org_dao.update(
            id=organization_id,
            name=organization.name,
            timezone=organization.timezone,
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

    # Check for billing blockers before allowing deletion
    ba = org.billing_account
    if ba:
        # Check for pending invoices
        pending_recharges = (
            session.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.status.in_(
                    [
                        RechargeStatus.PENDING_INVOICE,
                        RechargeStatus.INVOICE_CREATED,
                    ],
                ),
            )
            .all()
        )
        if pending_recharges:
            pending_amount = sum(r.amount_usd for r in pending_recharges)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Organization has ${pending_amount:.2f} in pending invoices. "
                "Please wait for invoices to be processed before deleting.",
            )

        # Check for open disputes
        disputed_recharges = (
            session.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.status == RechargeStatus.DISPUTED,
            )
            .first()
        )
        if disputed_recharges:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Organization has open payment disputes. "
                "Please wait for disputes to be resolved before deleting.",
            )

        # Check for problematic account status
        if ba.account_status in ("PAST_DUE", "SUSPENDED"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Organization billing account is {ba.account_status}. "
                "Please resolve outstanding billing issues before deleting.",
            )

    # Store Stripe customer ID for post-deletion archival
    stripe_customer_id = ba.stripe_customer_id if ba else None

    # Delete organization (cascades to related tables)
    try:
        org_dao.delete(organization_id)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete organization: {str(e)}",
        )

    # Archive Stripe customer (best-effort, don't fail if this errors)
    if stripe_customer_id:
        try:
            import stripe

            stripe.api_key = settings.stripe_secret_key
            stripe.Customer.modify(
                stripe_customer_id,
                metadata={
                    "organization_deleted": "true",
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except stripe.error.StripeError as e:
            # Log but don't fail - org is already deleted from DB
            logger.warning(
                f"Failed to archive Stripe customer {stripe_customer_id}: {e}",
            )

    return None


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

        # Grant Member access to Assistants project if it exists
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, org_member_dao, context_dao)
        assistants_projects = project_dao.filter(
            organization_id=organization_id,
            name="Assistants",
        )
        if assistants_projects:
            assistants_project = assistants_projects[0][0]
            member_role = role_dao.get_by_name("Member", organization_id=None)
            if member_role:
                resource_access_dao.grant_access(
                    resource_type="project",
                    resource_id=assistants_project.id,
                    role_id=member_role.id,
                    grantee_type="user",
                    grantee_id=member_data.user_id,
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

    Permission: self-removal OR org:write permission.
    - Any member can remove themselves (leave the organization)
    - Users with org:write permission can remove other members

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

    # Permission check: self-removal OR org:write
    is_self_removal = requesting_user_id == user_id
    has_admin_permission = resource_access_dao.check_org_member_permission(
        requesting_user_id,
        organization_id,
        "org:write",
    )
    if not (is_self_removal or has_admin_permission):
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

    # Remove member and clean up all associated data
    try:
        team_dao = TeamDAO(session)
        user_dao = UserDAO(session)
        contact_sync_service = ContactSyncService(session)

        # Get user info for Contact update
        user_row = user_dao.get_by_id(user_id)
        departing_user = user_row[0] if user_row else None

        # 1. Delete unshared resources created by this user
        resource_access_dao.delete_unshared_resources_by_creator(
            user_id,
            organization_id,
        )

        # 2. Remove user from all org teams
        team_dao.remove_user_from_all_org_teams(user_id, organization_id)

        # 3. Revoke resource access grants (for shared resources user had access to)
        resource_access_dao.revoke_user_access_for_organization(
            user_id,
            organization_id,
        )

        # 4. Mark user's Contact log as non-system (is_system=False)
        if departing_user and departing_user.email:
            contact_sync_service.mark_member_contact_as_non_system(
                organization_id=organization_id,
                email=departing_user.email,
            )

        # 5. Revoke organization API keys (personal keys are NOT affected)
        api_key_dao.revoke_organization_keys(
            user_id=user_id,
            organization_id=organization_id,
        )

        # 6. Remove member from organization
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
    user_dao = UserDAO(session)

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
        user_info_row = user_dao.get_by_id(member.user_id)
        user_name = None
        user_email = None
        user_image = None
        user_bio = None
        user_timezone = None
        user_phone_number = None
        if user_info_row:
            # get_by_id returns a Row, extract the User model
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
            user_bio = user_info.bio
            user_timezone = user_info.timezone
            user_phone_number = user_info.phone_number

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
                bio=user_bio,
                timezone=user_timezone,
                phone_number=user_phone_number,
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

        # Return updated member with user info
        updated_member = org_member_dao.get_member(member_user_id, organization_id)

        # Fetch user info
        user_dao = UserDAO(session)
        user_info_row = user_dao.get_by_id(member_user_id)
        user_name = None
        user_email = None
        user_image = None
        user_bio = None
        user_timezone = None
        user_phone_number = None
        if user_info_row:
            user_info = user_info_row[0]
            name_parts = []
            if user_info.name:
                name_parts.append(user_info.name)
            if user_info.last_name:
                name_parts.append(user_info.last_name)
            user_name = " ".join(name_parts) if name_parts else None
            user_email = user_info.email
            user_image = user_info.image
            user_bio = user_info.bio
            user_timezone = user_info.timezone
            user_phone_number = user_info.phone_number

        return OrganizationMemberResponse(
            id=updated_member.id,
            user_id=updated_member.user_id,
            organization_id=updated_member.organization_id,
            role_id=updated_member.role_id,
            role_name=role.name,
            created_at=updated_member.created_at,
            name=user_name,
            email=user_email,
            image=user_image,
            bio=user_bio,
            timezone=user_timezone,
            phone_number=user_phone_number,
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

        # Update organization: owner_id
        org_dao.update(
            id=organization_id,
            owner_id=transfer.new_owner_id,
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
    user_dao: UserDAO,
) -> InviteResponse:
    """Helper to build InviteResponse from invite object."""
    role_name = None
    if invite.role_id:
        role = role_dao.get(invite.role_id)
        role_name = role.name if role else None

    invited_by_name = None
    inviter_row = user_dao.get_by_id(invite.invited_by_user_id)
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
    user_dao = UserDAO(session)
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
    existing_user_row = user_dao.filter(email=email)
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
        await _send_invite_email(existing_invite, org, user_dao, user_id)
        return _build_invite_response(existing_invite, org, role_dao, user_dao)

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
        await _send_invite_email(invite, org, user_dao, user_id)

        return _build_invite_response(invite, org, role_dao, user_dao)

    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create invite: {str(e)}",
        )


async def _send_invite_email(
    invite,
    org,
    user_dao: UserDAO,
    inviter_user_id: str,
) -> None:
    """Send the invitation email."""
    # Get inviter info
    inviter_name = "A team member"
    inviter_row = user_dao.get_by_id(inviter_user_id)
    if inviter_row:
        inviter = inviter_row[0]
        if inviter.name:
            inviter_name = inviter.name
            if inviter.last_name:
                inviter_name += f" {inviter.last_name}"

    # Build invite link
    frontend_url = os.getenv(
        "UNIFY_CONSOLE_FRONTEND_URL",
        "https://console.unify.ai",
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
            send_email_async(
                invite.invitee_email,
                email_subject,
                email_body,
                from_email="hello@unify.ai",
            ),
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
    user_dao = UserDAO(session)
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
            _build_invite_response(invite, org, role_dao, user_dao)
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
    user_dao = UserDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    org_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    # Get current user's email
    user_row = user_dao.get_by_id(user_id)
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
                _build_invite_response(invite, org, role_dao, user_dao),
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
    user_dao = UserDAO(session)
    invite_dao = OrganizationInviteDAO(session)
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    api_key_dao = ApiKeyDAO(session)

    # Get current user
    user_row = user_dao.get_by_id(user_id)
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

        # Grant Member access to Assistants project if it exists
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, org_member_dao, context_dao)
        role_dao = RoleDAO(session)
        resource_access_dao = ResourceAccessDAO(session)
        assistants_projects = project_dao.filter(
            organization_id=invite.organization_id,
            name="Assistants",
        )
        if assistants_projects:
            assistants_project = assistants_projects[0][0]
            member_role = role_dao.get_by_name("Member", organization_id=None)
            if member_role:
                resource_access_dao.grant_access(
                    resource_type="project",
                    resource_id=assistants_project.id,
                    role_id=member_role.id,
                    grantee_type="user",
                    grantee_id=user_id,
                )

        # Delete the invite (accepted)
        invite_dao.delete_invite(invite)

        session.commit()

        # Trigger contact sync for all org assistants (non-blocking)
        from orchestra.db.dao.assistant_dao import AssistantDAO
        from orchestra.web.api.utils.assistant_infra import trigger_contact_sync

        assistant_dao = AssistantDAO(session)
        org_assistants = assistant_dao.list_all_org_assistants(
            organization_id=invite.organization_id,
        )

        for assistant in org_assistants:
            try:
                await trigger_contact_sync(assistant.agent_id)
                logger.info(
                    f"Triggered contact sync for assistant {assistant.agent_id}",
                )
            except Exception as e_sync:
                logger.warning(
                    f"Failed to trigger contact sync for assistant "
                    f"{assistant.agent_id}: {e_sync}",
                )

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
    user_dao = UserDAO(session)
    invite_dao = OrganizationInviteDAO(session)

    # Get current user
    user_row = user_dao.get_by_id(user_id)
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

    Returns credits, billing settings, and account status.
    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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

    ba = org.billing_account

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        credits=float(ba.credits) if ba else 0.0,
        stripe_customer_id=ba.stripe_customer_id if ba else None,
        autorecharge=ba.autorecharge if ba else False,
        autorecharge_threshold=float(ba.autorecharge_threshold) if ba else 0.0,
        autorecharge_qty=float(ba.autorecharge_qty) if ba else 25.0,
        account_status=ba.account_status if ba else "ACTIVE",
        billing_setup_complete=ba.billing_setup_complete if ba else False,
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
    import decimal

    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    ba_dao = BillingAccountDAO(session)
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

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    ba = org.billing_account
    if ba is None:
        ba = ba_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # Update settings directly on BillingAccount
    if billing_update.autorecharge is not None:
        ba.autorecharge = billing_update.autorecharge

    if billing_update.autorecharge_threshold is not None:
        ba.autorecharge_threshold = decimal.Decimal(
            str(billing_update.autorecharge_threshold),
        )

    if billing_update.autorecharge_qty is not None:
        qty = decimal.Decimal(str(billing_update.autorecharge_qty))
        if qty < MIN_AUTORECHARGE_AMOUNT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Minimum auto-recharge amount is ${MIN_AUTORECHARGE_AMOUNT}.",
            )
        ba.autorecharge_qty = qty

    session.commit()

    # Return updated billing info via BillingAccount
    session.refresh(org)
    ba = org.billing_account

    return OrganizationBillingResponse(
        organization_id=organization_id,
        organization_name=org.name,
        credits=float(ba.credits) if ba else 0.0,
        stripe_customer_id=ba.stripe_customer_id if ba else None,
        autorecharge=ba.autorecharge if ba else False,
        autorecharge_threshold=float(ba.autorecharge_threshold) if ba else 0.0,
        autorecharge_qty=float(ba.autorecharge_qty) if ba else 25.0,
        account_status=ba.account_status if ba else "ACTIVE",
        billing_setup_complete=ba.billing_setup_complete if ba else False,
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
    For orgs without billing configured, returns 0.
    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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

    # Get credits from organization's BillingAccount
    ba = org.billing_account
    has_direct = ba is not None and ba.stripe_customer_id is not None
    credits = float(ba.credits) if has_direct else 0.0

    return OrganizationCreditsResponse(
        organization_id=organization_id,
        credits=credits,
    ).model_dump()


@router.get(
    "/organizations/{organization_id}/billing/billing-profile",
    tags=["organization-billing"],
)
async def get_organization_billing_profile(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get business profile for an organization (invoicing information).

    Requires billing:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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

    # Get business profile directly from BillingAccount
    ba = org.billing_account
    profile = {
        "billing_email": ba.billing_email if ba else None,
        "business_name": ba.name if ba else None,
        "tax_id": ba.tax_id if ba else None,
        "billing_address": ba.billing_address if ba else None,
    }
    return OrganizationBusinessProfileResponse(**profile).model_dump()


@router.patch(
    "/organizations/{organization_id}/billing/billing-profile",
    tags=["organization-billing"],
)
async def update_organization_billing_profile(
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
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    ba_dao = BillingAccountDAO(session)
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

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    ba = org.billing_account
    if ba is None:
        ba = ba_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # Update profile
    billing_address_dict = None
    if profile_update.billing_address is not None:
        billing_address_dict = profile_update.billing_address.model_dump(
            exclude_none=True,
        )

        # Validate billing address fields
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        if (
            billing_address_dict.get("line1")
            or billing_address_dict.get(
                "city",
            )
            or billing_address_dict.get("country")
        ):
            is_valid, error_msg = validate_billing_address_data(
                line1=billing_address_dict.get("line1"),
                city=billing_address_dict.get("city"),
                country=billing_address_dict.get("country"),
                line2=billing_address_dict.get("line2"),
                state=billing_address_dict.get("state"),
                postal_code=billing_address_dict.get("postal_code"),
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid billing address: {error_msg}",
                )

    # Validate tax_id if provided along with country
    existing_billing_address = ba.billing_address
    if profile_update.tax_id is not None:
        # Get country from billing_address (either new or existing)
        country = None
        if billing_address_dict and billing_address_dict.get("country"):
            country = billing_address_dict["country"]
        elif existing_billing_address and existing_billing_address.get("country"):
            country = existing_billing_address["country"]

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

    # Update profile fields directly on BillingAccount
    if profile_update.billing_email is not None:
        ba.billing_email = profile_update.billing_email
    if profile_update.business_name is not None:
        ba.name = profile_update.business_name
    if profile_update.tax_id is not None:
        ba.tax_id = profile_update.tax_id
    if billing_address_dict is not None:
        ba.billing_address = billing_address_dict
    session.commit()

    # Sync changes to Stripe if org has a Stripe customer via BillingAccount
    stripe_cust_id = ba.stripe_customer_id if ba else None
    if stripe_cust_id:
        try:
            stripe_key = settings.stripe_secret_key
            if stripe_key:
                import stripe

                from orchestra.web.api.utils.business_validation import (
                    build_stripe_customer_name,
                    sync_tax_id_to_stripe,
                )

                stripe.api_key = stripe_key

                # Build update params for customer
                update_params: dict = {}

                if profile_update.billing_email:
                    update_params["email"] = profile_update.billing_email
                if profile_update.business_name:
                    update_params.update(
                        build_stripe_customer_name(
                            is_business=True,
                            name=profile_update.business_name,
                        ),
                    )
                if billing_address_dict and billing_address_dict.get("line1"):
                    update_params["address"] = {
                        "line1": billing_address_dict.get("line1", ""),
                        "line2": billing_address_dict.get("line2", ""),
                        "city": billing_address_dict.get("city", ""),
                        "state": billing_address_dict.get("state", ""),
                        "postal_code": billing_address_dict.get("postal_code", ""),
                        "country": billing_address_dict.get("country", ""),
                    }
                    # Validate location immediately when address changes
                    update_params["tax"] = {"validate_location": "immediately"}

                if update_params:
                    stripe.Customer.modify(stripe_cust_id, **update_params)

                # Sync tax ID if provided (uses shared helper that also
                # sets tax_exempt for B2B reverse-charge)
                if profile_update.tax_id is not None:
                    country_code = None
                    if billing_address_dict and billing_address_dict.get("country"):
                        country_code = billing_address_dict["country"]
                    elif existing_billing_address and existing_billing_address.get(
                        "country",
                    ):
                        country_code = existing_billing_address["country"]

                    sync_tax_id_to_stripe(
                        stripe_cust_id,
                        profile_update.tax_id,
                        country_code,
                        logger=logger,
                    )
        except Exception as e:
            # Log but don't fail - Stripe sync is best-effort
            logger.warning(
                f"Failed to sync business profile to Stripe for org {organization_id}: {e}",
            )

    # Return updated profile from the BillingAccount directly
    session.refresh(ba)
    return OrganizationBusinessProfileResponse(
        billing_email=ba.billing_email,
        business_name=ba.name,
        tax_id=ba.tax_id,
        billing_address=ba.billing_address,
    ).model_dump()


# ============================================================================
# Spending Limit Endpoints
# ============================================================================


@router.get(
    "/organizations/{organization_id}/spending-limit",
    response_model=OrgSpendingLimitResponse,
    responses={
        200: {
            "description": "Spending limit retrieved successfully",
        },
        403: {
            "description": "User is not a member of the organization",
        },
        404: {
            "description": "Organization not found",
        },
    },
)
async def get_org_spending_limit(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get the monthly spending limit for an organization.

    Returns the organization's limit. Any member of the organization can read this.
    """
    user_id = request_fastapi.state.user_id

    # Get the organization
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    # Check if user is a member of the org
    org_member_dao = OrganizationMemberDAO(session)
    member = org_member_dao.get_member(user_id, organization_id)
    is_owner = org.owner_id == user_id

    if not member and not is_owner:
        raise HTTPException(
            status_code=403,
            detail="You must be a member of this organization to view its spending limit.",
        )

    # Use DAO method for consistency
    spending_cap = org_dao.get_spending_cap(organization_id)

    return OrgSpendingLimitResponse(
        organization_id=organization_id,
        monthly_spending_cap=spending_cap,
        cascaded_updates=None,
    )


@router.put(
    "/organizations/{organization_id}/spending-limit",
    response_model=OrgSpendingLimitResponse,
    responses={
        200: {
            "description": "Spending limit set successfully",
            "content": {
                "application/json": {
                    "example": {
                        "organization_id": 1,
                        "monthly_spending_cap": 500.00,
                        "cascaded_updates": {"users_capped": 3, "assistants_capped": 7},
                    },
                },
            },
        },
        403: {
            "description": "User is not an admin of the organization",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Only organization admins can set spending limits.",
                    },
                },
            },
        },
        404: {
            "description": "Organization not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Organization not found."},
                },
            },
        },
    },
)
async def set_org_spending_limit(
    request_fastapi: Request,
    organization_id: int,
    body: OrgSpendingLimitRequest,
    session: Session = Depends(get_db_session),
):
    """
    Set the monthly spending limit for an organization.

    When the limit is lowered, member and assistant limits that exceed the new org limit
    will be automatically capped to the org limit (eager cascade).

    Setting to null removes the limit (no cap for members/assistants from this org).
    """
    user_id = request_fastapi.state.user_id

    # Get the organization
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    # Check if user has org:write permission via org membership role
    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )

    if not has_permission:
        raise HTTPException(
            status_code=403,
            detail="Only organization admins can set spending limits.",
        )

    # Use the DAO method which handles cascade logic
    cascade_result = org_dao.set_spending_cap(
        org_id=organization_id,
        monthly_spending_cap=body.monthly_spending_cap,
    )
    session.commit()

    cascaded_updates = None
    if cascade_result.members_capped > 0 or cascade_result.assistants_capped > 0:
        cascaded_updates = {
            "members_capped": cascade_result.members_capped,
            "assistants_capped": cascade_result.assistants_capped,
        }

    return OrgSpendingLimitResponse(
        organization_id=organization_id,
        monthly_spending_cap=body.monthly_spending_cap,
        cascaded_updates=cascaded_updates,
    )


@router.put(
    "/organizations/{organization_id}/members/{member_user_id}/spending-limit",
    response_model=MemberSpendingLimitResponse,
    responses={
        200: {
            "description": "Member spending limit set successfully",
        },
        400: {
            "description": "Member limit exceeds organization limit",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Member limit cannot exceed organization limit of $500.00",
                    },
                },
            },
        },
        403: {
            "description": "User is not an admin of the organization",
        },
        404: {
            "description": "Organization or member not found",
        },
    },
)
async def set_member_spending_limit(
    request_fastapi: Request,
    organization_id: int,
    member_user_id: str,
    body: MemberSpendingLimitRequest,
    session: Session = Depends(get_db_session),
) -> MemberSpendingLimitResponse:
    """
    Set the monthly spending limit for a member within an organization.

    This limit controls how much the member can spend when using the org's API key.
    It is separate from the user's personal spending limit (which applies to their
    personal API key).

    The member limit cannot exceed the organization's limit.
    When the limit is lowered, assistant limits owned by this member that exceed
    the new limit will be automatically capped.
    """
    user_id = request_fastapi.state.user_id

    # Get the organization
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    # Check if user has org:write permission
    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=403,
            detail="Only organization admins can set member spending limits.",
        )

    # Get the member
    org_member_dao = OrganizationMemberDAO(session)
    member = org_member_dao.get_member(member_user_id, organization_id)
    if not member:
        raise HTTPException(
            status_code=404,
            detail="User is not a member of this organization.",
        )

    org_spending_cap = (
        float(org.monthly_spending_cap) if org.monthly_spending_cap else None
    )

    try:
        cascade_result = org_member_dao.set_spending_cap(
            user_id=member_user_id,
            organization_id=organization_id,
            monthly_spending_cap=body.monthly_spending_cap,
            org_spending_cap=org_spending_cap,
        )
        session.commit()

        return MemberSpendingLimitResponse(
            organization_id=organization_id,
            user_id=member_user_id,
            monthly_spending_cap=body.monthly_spending_cap,
            assistants_capped=cascade_result.assistants_capped,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/organizations/{organization_id}/members/{member_user_id}/spending-limit",
    response_model=MemberSpendingLimitResponse,
)
async def get_member_spending_limit(
    request_fastapi: Request,
    organization_id: int,
    member_user_id: str,
    session: Session = Depends(get_db_session),
) -> MemberSpendingLimitResponse:
    """
    Get the monthly spending limit for a member within an organization.
    """
    user_id = request_fastapi.state.user_id

    # Get the organization
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    # Check if user has org:read permission
    resource_access_dao = ResourceAccessDAO(session)
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:read",
    )
    if not has_permission:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view member spending limits.",
        )

    # Get the member and verify they exist
    org_member_dao = OrganizationMemberDAO(session)
    member = org_member_dao.get_member(member_user_id, organization_id)
    if not member:
        raise HTTPException(
            status_code=404,
            detail="User is not a member of this organization.",
        )

    # Use DAO method for consistency
    spending_cap = org_member_dao.get_spending_cap(member_user_id, organization_id)

    return MemberSpendingLimitResponse(
        organization_id=organization_id,
        user_id=member_user_id,
        monthly_spending_cap=spending_cap,
        assistants_capped=0,
    )


# ============================================================================
# Organization Stripe Customer Endpoints
# ============================================================================


@router.post(
    "/organizations/{organization_id}/billing/stripe-customer",
    tags=["organization-billing"],
    response_model=dict,
)
async def ensure_organization_stripe_customer(
    request_fastapi: Request,
    organization_id: int,
    body: Optional[OrganizationStripeCustomerCreateRequest] = Body(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Ensure a Stripe customer exists for an organization.

    This endpoint creates a Stripe customer for the organization if one doesn't
    exist, or returns the existing customer ID. This enables direct billing
    for the organization.

    Requires billing:write permission (Owners and Admins).

    The organization must have a billing_email set (either in business profile
    or provided in the request body) for Stripe customer creation.

    Returns:
        - organization_id: The organization's ID
        - stripe_customer_id: The Stripe customer ID
        - is_new: True if the customer was just created, False if it existed
    """

    import stripe

    from orchestra.web.api.organization.schema import OrganizationStripeCustomerResponse

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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
            detail="You do not have permission to manage billing for this organization",
        )

    # Get billing account (created eagerly in organization_dao.create;
    # fallback handles legacy orgs that may not have one yet)
    billing_account_dao = BillingAccountDAO(session)
    ba = org.billing_account
    if ba is None:
        ba = billing_account_dao.create()
        org.billing_account_id = ba.id
        session.flush()

    # If Stripe customer already exists, return it
    if ba.stripe_customer_id:
        return OrganizationStripeCustomerResponse(
            organization_id=organization_id,
            stripe_customer_id=ba.stripe_customer_id,
            is_new=False,
        ).model_dump()

    # Determine email for Stripe customer
    billing_email = None
    if body and body.billing_email:
        billing_email = body.billing_email
    elif ba.billing_email:
        billing_email = ba.billing_email
    else:
        # Fall back to owner's email
        user_dao = UserDAO(session)
        owner = user_dao.get_by_id(org.owner_id)
        if owner:
            billing_email = owner[0].email

    if not billing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization must have a billing_email set or provide one in the request",
        )

    # Determine name for Stripe customer
    business_name = None
    if body and body.business_name:
        business_name = body.business_name
    elif ba.name:
        business_name = ba.name
    else:
        business_name = org.name  # Fall back to org name

    # Configure Stripe
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe is not configured",
        )

    stripe.api_key = settings.stripe_secret_key

    try:
        # Build Stripe customer params including address and tax ID if available
        from orchestra.web.api.utils.business_validation import (
            build_stripe_customer_name,
            get_stripe_tax_exempt_status,
            get_stripe_tax_id_data,
        )

        customer_params = {
            "email": billing_email,
            **build_stripe_customer_name(is_business=True, name=business_name),
            "metadata": {
                "organization_id": str(organization_id),
                "organization_name": org.name,
                "billing_account_id": str(ba.id),
            },
        }

        # Sync billing address to Stripe if available (from BillingAccount)
        ba_address = ba.billing_address or {}
        if ba_address.get("line1"):
            customer_params["address"] = {
                "line1": ba_address.get("line1", ""),
                "line2": ba_address.get("line2", ""),
                "city": ba_address.get("city", ""),
                "state": ba_address.get("state", ""),
                "postal_code": ba_address.get("postal_code", ""),
                "country": ba_address.get("country", ""),
            }
            # Validate location immediately for tax calculations
            customer_params["tax"] = {"validate_location": "immediately"}

        # Sync tax ID to Stripe if available
        country_code = ba_address.get("country")
        tax_id_data = get_stripe_tax_id_data(ba.tax_id, country_code)
        if tax_id_data:
            customer_params["tax_id_data"] = tax_id_data

        # Set tax_exempt based on B2B tax ID status
        customer_params["tax_exempt"] = get_stripe_tax_exempt_status(
            ba.tax_id,
            country_code,
        )

        # Create Stripe customer
        customer = stripe.Customer.create(**customer_params)

        # Store the Stripe customer ID on the BillingAccount
        ba.stripe_customer_id = customer.id

        # Update business profile if provided in request
        if body:
            if body.billing_email and body.billing_email != ba.billing_email:
                ba.billing_email = body.billing_email
            if body.business_name and body.business_name != ba.name:
                ba.name = body.business_name

        session.commit()

        return OrganizationStripeCustomerResponse(
            organization_id=organization_id,
            stripe_customer_id=customer.id,
            is_new=True,
        ).model_dump()

    except stripe.error.StripeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create Stripe customer: {str(e)}",
        )


@router.get(
    "/organizations/{organization_id}/billing/stripe-customer",
    tags=["organization-billing"],
)
async def get_organization_stripe_customer(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Get the Stripe customer ID for an organization.

    Returns the Stripe customer ID if one exists, or indicates if direct
    billing is not yet set up.

    Requires billing:read permission.
    """
    from orchestra.web.api.organization.schema import OrganizationStripeCustomerResponse

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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
            detail="You do not have permission to view billing info for this organization",
        )

    ba = org.billing_account
    stripe_cust_id = ba.stripe_customer_id if ba else None
    if not stripe_cust_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization does not have direct billing set up. "
            "Use POST to create a Stripe customer.",
        )

    return OrganizationStripeCustomerResponse(
        organization_id=organization_id,
        stripe_customer_id=stripe_cust_id,
        is_new=False,
    ).model_dump()


@router.post(
    "/organizations/{organization_id}/billing/checkout",
    tags=["organization-billing"],
    response_model=dict,
)
async def create_organization_checkout_session(
    request_fastapi: Request,
    organization_id: int,
    checkout_request: "OrganizationCheckoutRequest",
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Create a Stripe checkout session for purchasing credits for an organization.

    This endpoint creates a one-time payment checkout session. Upon successful
    payment, credits will be added to the organization's balance.

    The organization must have a Stripe customer set up first (via the
    ensure-stripe-customer endpoint).

    Requires billing:write permission (Owners and Admins).

    Args:
        amount: Amount of credits to purchase (1 credit = $1)
        success_url: URL to redirect to on successful payment
        cancel_url: URL to redirect to on cancelled payment

    Returns:
        - checkout_url: URL to redirect the user to for payment
        - session_id: Stripe checkout session ID
    """

    import stripe

    from orchestra.web.api.organization.schema import OrganizationCheckoutResponse

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
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
            detail="You do not have permission to manage billing for this organization",
        )

    # Check if organization has Stripe customer via BillingAccount
    ba = org.billing_account
    if not ba or not ba.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization must have a Stripe customer set up first. "
            "Use POST /billing/stripe-customer to create one.",
        )

    # Validate amount
    if checkout_request.amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Amount must be greater than 0",
        )

    # Configure Stripe
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe is not configured",
        )

    stripe.api_key = settings.stripe_secret_key

    try:
        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            submit_type="pay",
            customer=ba.stripe_customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": 100,  # $1 = 100 cents = 1 credit
                        "product_data": {
                            "name": "Unify Credits",
                            "description": f"Credits for {org.name}",
                        },
                    },
                    "quantity": checkout_request.amount,
                },
            ],
            automatic_tax={"enabled": True},
            customer_update={
                "address": "auto",
                "name": "auto",
            },
            billing_address_collection="required",
            tax_id_collection={"enabled": True},
            success_url=checkout_request.success_url,
            cancel_url=checkout_request.cancel_url,
            metadata={
                "organization_id": str(organization_id),
                "organization_name": org.name,
                "credits_purchased": str(checkout_request.amount),
                "initiated_by_user_id": user_id,
            },
            payment_intent_data={
                "metadata": {
                    "organization_id": str(organization_id),
                    "credits_purchased": str(checkout_request.amount),
                },
            },
            payment_method_options={
                "card": {"request_three_d_secure": "any"},
            },
            invoice_creation={
                "enabled": True,
                "invoice_data": {
                    "description": f"Unify Credits purchase ({checkout_request.amount} credits) for {org.name}",
                },
            },
        )

        if not checkout_session.url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create checkout session URL",
            )

        return OrganizationCheckoutResponse(
            checkout_url=checkout_session.url,
            session_id=checkout_session.id,
        ).model_dump()

    except stripe.error.StripeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create checkout session: {str(e)}",
        )


# ============================================================================
# Admin Spend Endpoints (for UniLLM service calls)
# ============================================================================


@admin_router.get("/organization/{organization_id}/spend")
def admin_get_org_spend(
    organization_id: int,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
):
    """
    Admin endpoint: Get an organization's cumulative spend for a given month.

    This endpoint is for internal service calls (e.g., UniLLM) and does not
    require the caller to be a member of the organization.
    """
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    cumulative_spend = org_dao.get_cumulative_spend(organization_id, month)
    limit = org_dao.get_spending_cap(organization_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    return OrgSpendResponse(
        organization_id=organization_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=org.monthly_spending_cap_set_at,
        percent_used=percent_used,
    )


@admin_router.get("/organization/{organization_id}/members/{member_user_id}/spend")
def admin_get_member_spend(
    organization_id: int,
    member_user_id: str,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
):
    """
    Admin endpoint: Get an organization member's cumulative spend for a given month.

    This endpoint is for internal service calls (e.g., UniLLM) and does not
    require the caller to have org membership.

    The spend is the SUM of all assistant spending logs for this user in the org.
    """
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    org_member_dao = OrganizationMemberDAO(session)
    member = org_member_dao.get_member(member_user_id, organization_id)
    if not member:
        raise HTTPException(
            status_code=404,
            detail="User is not a member of this organization.",
        )

    cumulative_spend = org_member_dao.get_cumulative_spend(
        member_user_id,
        organization_id,
        month,
    )
    limit = org_member_dao.get_spending_cap(member_user_id, organization_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    return MemberSpendResponse(
        organization_id=organization_id,
        user_id=member_user_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=member.monthly_spending_cap_set_at,
        percent_used=percent_used,
    )


@admin_router.put("/organization/{organization_id}/verify")
def admin_verify_organization(
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Mark an organization as verified.

    Verified organizations receive higher rate limits for assistant-related
    endpoints. This is a manual verification process performed by an admin
    after reviewing the organization.

    Returns the updated organization info including verification status.
    """
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    if org.verified:
        return {
            "message": "Organization is already verified.",
            "organization_id": organization_id,
            "name": org.name,
            "verified": org.verified,
            "verified_at": org.verified_at.isoformat() if org.verified_at else None,
        }

    # Mark as verified
    org.verified = True
    org.verified_at = datetime.now(timezone.utc)
    session.commit()

    return {
        "message": "Organization verified successfully.",
        "organization_id": organization_id,
        "name": org.name,
        "verified": org.verified,
        "verified_at": org.verified_at.isoformat(),
    }


@admin_router.delete("/organization/{organization_id}/verify")
def admin_unverify_organization(
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Remove verification status from an organization.

    This revokes the higher rate limits associated with verified status.
    """
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    if not org.verified:
        return {
            "message": "Organization is not verified.",
            "organization_id": organization_id,
            "name": org.name,
            "verified": False,
            "verified_at": None,
        }

    # Remove verification
    org.verified = False
    org.verified_at = None
    session.commit()

    return {
        "message": "Organization verification removed.",
        "organization_id": organization_id,
        "name": org.name,
        "verified": False,
        "verified_at": None,
    }


@admin_router.get("/organization/{organization_id}/verification")
def admin_get_organization_verification(
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get the verification status of an organization.
    """
    org_dao = OrganizationDAO(session)
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")

    return {
        "organization_id": organization_id,
        "name": org.name,
        "verified": org.verified,
        "verified_at": org.verified_at.isoformat() if org.verified_at else None,
    }
