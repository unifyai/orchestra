"""Organization management endpoints."""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
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
from orchestra.db.models.orchestra_models import Assistant, Recharge, RechargeStatus
from orchestra.services.bucket_service import BucketService
from orchestra.services.contact_sync_service import ContactSyncService
from orchestra.web.api.organization.schema import (
    AcceptInviteResponse,
    DeclineInviteResponse,
    InviteListResponse,
    InviteResponse,
    InviteUserRequest,
    MemberSpendingLimitRequest,
    MemberSpendingLimitResponse,
    MemberSpendResponse,
    MFAEnforcementStatusResponse,
    OrganizationCreate,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberRoleUpdate,
    OrganizationOwnershipTransfer,
    OrganizationResponse,
    OrganizationUpdate,
    OrgMFASettingsRequest,
    OrgMFASettingsResponse,
    OrgSpendingLimitRequest,
    OrgSpendingLimitResponse,
    OrgSpendResponse,
)
from orchestra.web.api.users.views import generate_key
from orchestra.web.api.utils.email import send_email_async
from orchestra.web.api.utils.mfa_enforcement import check_org_mfa_enforcement

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
    resource_access_dao = ResourceAccessDAO(session)

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

    members = org_member_dao.get_members_with_details(organization_id)
    return [OrganizationMemberResponse(**m) for m in members]


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
    _: None = Depends(check_org_mfa_enforcement()),
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


@router.post(
    "/organizations/{organization_id}/photo/upload",
    status_code=status.HTTP_201_CREATED,
    summary="Upload organization photo",
    tags=["Organizations"],
)
async def upload_org_photo(
    request_fastapi: Request,
    organization_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    _: None = Depends(check_org_mfa_enforcement()),
):
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

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

    ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if not file.content_type or file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_IMAGE_TYPES)}",
        )

    MAX_SIZE_BYTES = 5 * 1024 * 1024
    file_content = await file.read()
    if len(file_content) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds {MAX_SIZE_BYTES // (1024 * 1024)}MB limit.",
        )

    bucket_service = BucketService()
    gcs_url = bucket_service.upload_org_photo_file(
        file_content=file_content,
        org_id=organization_id,
        content_type=file.content_type,
    )

    org.image = gcs_url
    session.commit()

    return {"gcs_url": gcs_url}


@router.delete(
    "/organizations/{organization_id}/photo",
    summary="Remove organization photo",
    tags=["Organizations"],
)
async def remove_org_photo(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
    _: None = Depends(check_org_mfa_enforcement()),
):
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

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

    # Delete all photos for this org from the account photo bucket
    try:
        bucket_service = BucketService()
        bucket_service.delete_org_account_photos(organization_id)
    except Exception as e:
        logger.error(
            f"Failed to delete GCS photos for org {organization_id}: {e}",
        )

    org.image = None
    session.commit()

    return {"message": "Organization photo removed."}


@router.delete(
    "/organizations/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_organization(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
    _: None = Depends(check_org_mfa_enforcement()),
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

    # Collect all assistant IDs *before* DB deletion (CASCADE removes them)
    org_assistant_ids: list[int] = [
        a.agent_id
        for a in session.query(Assistant.agent_id)
        .filter(Assistant.organization_id == organization_id)
        .all()
    ]

    # Deprovision and soft-delete all contacts for org assistants *before*
    # CASCADE deletes the rows.  Best-effort – don't block org deletion.
    if org_assistant_ids:
        try:
            from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
            from orchestra.routines.assistant_contact_suspension import (
                _deprovision_contact,
            )

            contact_dao = AssistantContactDAO(session)
            active_contacts = contact_dao.get_active_contacts_for_assistants(
                org_assistant_ids,
            )
            for contact in active_contacts:
                try:
                    import asyncio

                    asyncio.get_event_loop().run_until_complete(
                        _deprovision_contact(contact),
                    )
                except RuntimeError:
                    import asyncio

                    asyncio.run(_deprovision_contact(contact))
                except Exception as e:
                    logger.error(
                        f"Failed to deprovision {contact.contact_type} "
                        f"({contact.contact_value}) for org {organization_id}: {e}",
                    )
            contact_dao.soft_delete_contacts_for_organization(organization_id)
        except Exception as e:
            logger.error(
                f"Failed to deprovision contacts for org {organization_id}: {e}",
            )

    # Delete organization (cascades to related tables)
    try:
        org_dao.delete(organization_id)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete organization: {str(e)}",
        )

    # Post-commit: clean up GCS data for every assistant that was in this org
    try:
        bucket_service = BucketService()

        if org_assistant_ids:
            for aid in org_assistant_ids:
                try:
                    bucket_service.delete_all_assistant_data(aid)
                except Exception as e:
                    logger.error(
                        f"Failed to clean up GCS data for assistant {aid} "
                        f"(org {organization_id}): {e}",
                    )
            logger.info(
                f"Cleaned up GCS data for {len(org_assistant_ids)} assistant(s) "
                f"in deleted org {organization_id}",
            )

        # Clean up org account photos from the dedicated account photo bucket
        try:
            photo_count = bucket_service.delete_org_account_photos(organization_id)
            if photo_count > 0:
                logger.info(
                    f"Cleaned up {photo_count} account photo(s) for "
                    f"org {organization_id}",
                )
        except Exception as e:
            logger.error(
                f"Failed to clean up account photos for org {organization_id}: {e}",
            )
    except Exception as e:
        logger.error(
            f"Failed to initialize BucketService for org {organization_id} "
            f"GCS cleanup: {e}",
        )

    # Archive Stripe customer (best-effort, don't fail if this errors)
    if stripe_customer_id:
        try:
            import stripe

            from orchestra.lib.billing import configure_stripe

            configure_stripe()
            stripe.Customer.modify(
                stripe_customer_id,
                metadata={
                    "organization_deleted": "true",
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except stripe.StripeError as e:
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
    _: None = Depends(check_org_mfa_enforcement()),
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
    _: None = Depends(check_org_mfa_enforcement()),
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

    # Collect assistant IDs for this user in this org *before* they may be
    # deleted by delete_unshared_resources_by_creator (needed for GCS cleanup).
    member_assistant_ids: list[int] = [
        a.agent_id
        for a in session.query(Assistant.agent_id)
        .filter(
            Assistant.user_id == user_id,
            Assistant.organization_id == organization_id,
        )
        .all()
    ]

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
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove member: {str(e)}",
        )

    # Post-commit: clean up GCS data for deleted assistants (best-effort).
    # Only assistants that were actually removed from the DB need cleanup.
    # After commit, check which assistant IDs no longer exist.
    if member_assistant_ids:
        surviving_ids = {
            a.agent_id
            for a in session.query(Assistant.agent_id)
            .filter(Assistant.agent_id.in_(member_assistant_ids))
            .all()
        }
        deleted_assistant_ids = [
            aid for aid in member_assistant_ids if aid not in surviving_ids
        ]
        if deleted_assistant_ids:
            try:
                bucket_service = BucketService()
                for aid in deleted_assistant_ids:
                    try:
                        bucket_service.delete_all_assistant_data(aid)
                    except Exception as e:
                        logger.error(
                            f"Failed to clean up GCS data for assistant {aid} "
                            f"(member removal, org {organization_id}): {e}",
                        )
                logger.info(
                    f"Cleaned up GCS data for {len(deleted_assistant_ids)} "
                    f"assistant(s) after removing member {user_id} from "
                    f"org {organization_id}",
                )
            except Exception as e:
                logger.error(
                    f"Failed to initialize BucketService for member removal "
                    f"GCS cleanup (user {user_id}, org {organization_id}): {e}",
                )

    return None


@router.get(
    "/organizations/{organization_id}/members",
    response_model=List[OrganizationMemberResponse],
)
async def list_organization_members(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
    _: None = Depends(check_org_mfa_enforcement()),
) -> List[OrganizationMemberResponse]:
    """
    List all members of an organization with their roles.

    Requires org:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

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

    members = org_member_dao.get_members_with_details(organization_id)
    return [OrganizationMemberResponse(**m) for m in members]


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
    _: None = Depends(check_org_mfa_enforcement()),
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

        # Return updated member with user info (single JOIN query)
        member_dict = org_member_dao.get_member_with_details(
            member_user_id,
            organization_id,
        )
        if not member_dict:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member not found after update",
            )
        return OrganizationMemberResponse(**member_dict)
    except HTTPException:
        raise
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
    _: None = Depends(check_org_mfa_enforcement()),
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
    _: None = Depends(check_org_mfa_enforcement()),
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
    invite_link = f"{frontend_url}/login/invite?token={invite.token}"

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
        sent = await send_email_async(
            invite.invitee_email,
            email_subject,
            email_body,
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if sent:
            logger.info(f"Invite email sent to {invite.invitee_email}")
        else:
            print(
                f"[LOCAL DEV] Invite link for {invite.invitee_email}: {invite_link}",
                flush=True,
            )
    except Exception as e:
        logger.error(f"Failed to send invite email to {invite.invitee_email}: {e}")
        print(
            f"[LOCAL DEV] Invite link for {invite.invitee_email}: {invite_link}",
            flush=True,
        )


@router.get(
    "/organizations/{organization_id}/invites",
    response_model=InviteListResponse,
)
async def list_organization_invites(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
    _: None = Depends(check_org_mfa_enforcement()),
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
    _: None = Depends(check_org_mfa_enforcement()),
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

        # Mark user as onboarded (they're joining via invite, no workspace selection needed)
        from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO

        onboarding_dao = OnboardingStatusDAO(session)
        onboarding_status = onboarding_dao.get_by_user_id(user_id)
        if onboarding_status and onboarding_status.current_step != "completed":
            onboarding_dao.mark_completed(user_id)

        # Check if org requires MFA and user hasn't set it up
        mfa_setup_required = False
        if org.require_mfa:
            from orchestra.db.dao.mfa_credential_dao import MFACredentialDAO

            mfa_cred_dao = MFACredentialDAO(session)
            if not mfa_cred_dao.has_enabled_mfa(user_id):
                mfa_setup_required = True

        return AcceptInviteResponse(
            message="Successfully joined organization",
            organization_id=org.id,
            organization_name=org.name,
            api_key=new_api_key,
            mfa_setup_required=mfa_setup_required,
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

    # Include credit balance from the billing account (for credit guard checks)
    credit_balance = None
    if org.billing_account:
        credit_balance = float(org.billing_account.credits)

    return OrgSpendResponse(
        organization_id=organization_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=org.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
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

    # Include credit balance from the org's billing account (for credit guard checks)
    credit_balance = None
    if org.billing_account:
        credit_balance = float(org.billing_account.credits)

    return MemberSpendResponse(
        organization_id=organization_id,
        user_id=member_user_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=member.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
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


# =============================================================================
# Organization MFA Enforcement
# =============================================================================


@router.get(
    "/organizations/{organization_id}/mfa-settings",
    response_model=OrgMFASettingsResponse,
    status_code=status.HTTP_200_OK,
)
def get_org_mfa_settings(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get MFA enforcement settings for an organization.

    Requires org:read permission.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

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

    settings = org_dao.get_mfa_settings(organization_id)
    return OrgMFASettingsResponse(**settings)


@router.put(
    "/organizations/{organization_id}/mfa-settings",
    response_model=OrgMFASettingsResponse,
    status_code=status.HTTP_200_OK,
)
def update_org_mfa_settings(
    request_fastapi: Request,
    organization_id: int,
    body: OrgMFASettingsRequest,
    session: Session = Depends(get_db_session),
):
    """
    Update MFA enforcement settings for an organization.

    Requires org:write permission (Owner, Admin roles).
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

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

    result = org_dao.update_mfa_settings(
        org_id=organization_id,
        require_mfa=body.require_mfa,
    )
    session.commit()

    return OrgMFASettingsResponse(**result)


@admin_router.get(
    "/auth/mfa-enforcement-status",
    response_model=MFAEnforcementStatusResponse,
    status_code=status.HTTP_200_OK,
)
def mfa_enforcement_status(
    user_id: str,
    org_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Check whether a user must set up MFA to access a given organization.

    Called by the Next.js server (admin-key auth) during workspace
    resolution to decide if the user should be redirected to MFA setup.

    MFA enforcement applies to all members regardless of auth provider
    (email/password, Google, GitHub). If the org requires MFA and the
    user hasn't set it up, ``setup_required`` is True.
    """
    from orchestra.db.dao.mfa_credential_dao import MFACredentialDAO

    org_dao = OrganizationDAO(session)
    org = org_dao.get(org_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {org_id} not found",
        )

    enforced = org.require_mfa

    mfa_dao = MFACredentialDAO(session)
    has_mfa = mfa_dao.has_enabled_mfa(user_id)

    setup_required = enforced and not has_mfa

    return MFAEnforcementStatusResponse(
        enforced=enforced,
        has_mfa=has_mfa,
        setup_required=setup_required,
    )
