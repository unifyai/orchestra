import base64
import datetime
import logging
import secrets
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.one_time_credit_grant_link_dao import OneTimeCreditGrantLinkDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.services.user_account_cleanup_service import UserAccountCleanupService
from orchestra.settings import settings
from orchestra.web.api.users.schema import (
    AccountDeletionConfirmation,
    AccountDeletionResponse,
    AccountRequest,
    CanDeleteAccountResponse,
    CreditGrantClaimResponse,
    CreditGrantLinkClaimRequest,
    CreditGrantLinkCreateRequest,
    CreditGrantLinkResponse,
    DeletionBlockerResponse,
    OnboardingStatusDetailedResponse,
    OnboardingStatusResponse,
    OnboardingStatusUpdateRequest,
    OnboardingStepDataResponse,
    QueryLoggingStatus,
    UpdateOnboardingStatusRequest,
    UpdateQueryLoggingRequest,
    UserBillingProfileResponse,
    UserBillingProfileUpdate,
    UserCheckoutRequest,
    UserCheckoutResponse,
    UserRequest,
    UserSpendingLimitRequest,
    UserSpendingLimitResponse,
    UserSpendResponse,
)
from orchestra.web.api.utils.http_responses import not_found
from orchestra.web.api.utils.tax_id_validator import (
    TaxIDValidator,
    validate_tax_id_for_country,
)

admin_router = APIRouter()
router = APIRouter()
logger = logging.getLogger(__name__)

# TODO: Move exceptions to exceptions file
# TODO: Fetch organization if it exists when reading user info
# TODO: Return tier in user info endpoints + double check rest of the information

# Endpoints used by next-auth


@admin_router.post("/user")
@admin_router.post("/auth-user")  # backward-compat alias
def create_user(
    user: UserRequest,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)

    user_dao.create(
        email=user.email,
        name=user.name,
        last_name=user.last_name,
        job_title=user.job_title,
        bio=user.bio,
        image=user.image,
        timezone=user.timezone,
        phone_number=user.phone_number,
    )
    user_row = user_dao.filter(email=user.email)
    new_user = user_row[0][0]

    new_api_key = generate_key()
    api_key_dao.create(key=new_api_key, name="", user_id=new_user.id)

    # Seed default Unity project, interface, tab, and table tile for tasks
    try:
        DefaultTasksSeeder.seed(session, user_id=new_user.id)
    except Exception as e:
        print(e)
    return {
        "id": new_user.id,
        "name": new_user.name,
        "bio": new_user.bio,
        "image": new_user.image,
        "email": new_user.email,
        "timezone": new_user.timezone,
        "phone_number": new_user.phone_number,
    }


@admin_router.get("/user/by-user-id")
@admin_router.get("/auth-user/by-user-id")  # backward-compat alias
def get_user(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    user = user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]

    # Build organizations list with org-specific API keys
    organizations = user_dao.get_user_organizations(
        user_instance.id,
        organization_dao,
        organization_member_dao,
        api_key_dao,
        role_dao,
    )

    has_claimed = OneTimeCreditGrantLinkDAO(session).has_user_claimed_any_link(
        user_instance.id,
    )
    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "last_name": user_instance.last_name,
        "job_title": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "created_at": user_instance.created_at,
        "api_key": api_key_instance.key,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        # backward-compat aliases (approval flow removed; always approved)
        "assistant_hiring_approval": "approved",
        "has_claimed_approval_link": has_claimed,
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.get("/user/by-email")
@admin_router.get("/auth-user/by-email")  # backward-compat alias
def get_user_by_email(
    email: str,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    user = user_dao.filter(email=email)
    if not user:
        return None
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]

    # Build organizations list with org-specific API keys
    organizations = user_dao.get_user_organizations(
        user_instance.id,
        organization_dao,
        organization_member_dao,
        api_key_dao,
        role_dao,
    )

    has_claimed = OneTimeCreditGrantLinkDAO(session).has_user_claimed_any_link(
        user_instance.id,
    )
    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "last_name": user_instance.last_name,
        "job_title": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "created_at": user_instance.created_at,
        "api_key": api_key_instance.key,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        # backward-compat aliases (approval flow removed; always approved)
        "assistant_hiring_approval": "approved",
        "has_claimed_approval_link": has_claimed,
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.get("/user/by-account")
@admin_router.get("/auth-user/by-account")  # backward-compat alias
def get_user_by_account(
    provider_account_id: str,
    provider: str,
    session: Session = Depends(get_db_session),
):
    account_dao = AccountDAO(session)
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    account = account_dao.filter(
        provider_account_id=provider_account_id,
        provider=provider,
    )
    if not account:
        return None
    user = user_dao.filter(id=account[0][0].user_id)
    if not user:
        return None
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]
    # Build organizations list with org-specific API keys
    organizations = user_dao.get_user_organizations(
        user_instance.id,
        organization_dao,
        organization_member_dao,
        api_key_dao,
        role_dao,
    )

    has_claimed = OneTimeCreditGrantLinkDAO(session).has_user_claimed_any_link(
        user_instance.id,
    )
    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "last_name": user_instance.last_name,
        "job_title": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "created_at": user_instance.created_at,
        "api_key": api_key_instance.key,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        # backward-compat aliases (approval flow removed; always approved)
        "assistant_hiring_approval": "approved",
        "has_claimed_approval_link": has_claimed,
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.put("/user")
@admin_router.put("/auth-user")  # backward-compat alias
def update_user(
    updated_user: UserRequest,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    user = user_dao.filter(id=updated_user.user_id)
    if not user:
        raise not_found("User")
    user_dao.update(
        id=updated_user.user_id,
        name=updated_user.name,
        last_name=updated_user.last_name,
        job_title=updated_user.job_title,
        bio=updated_user.bio,
        timezone=updated_user.timezone,
        phone_number=updated_user.phone_number,
    )
    return "User information updated successfully!"


@admin_router.delete("/user", response_model=AccountDeletionResponse)
@admin_router.delete(
    "/auth-user",
    response_model=AccountDeletionResponse,
)  # backward-compat alias
def delete_user(
    user_id: str,
    force: bool = Query(
        False,
        description="Skip organization ownership check (use with caution)",
    ),
    session: Session = Depends(get_db_session),
):
    """
    Delete a user account and all associated data (admin endpoint).

    This performs a complete cleanup:
    - All user data across all tables
    - Billing records (recharges)
    - Projects, API keys, queries, etc.
    - Archives Stripe customer (preserves invoice history)

    Blocked if user has pending bills unless resolved first.
    Use force=True to skip organization ownership check.
    """
    cleanup_service = UserAccountCleanupService(session)
    result = cleanup_service.delete_user_account(user_id, force_org_check=force)

    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)

    return AccountDeletionResponse(success=True, message=result.message)


@admin_router.post("/account")
def link_account(
    account: AccountRequest,
    session: Session = Depends(get_db_session),
):
    account_dao = AccountDAO(session)
    account_dao.create(
        user_id=account.user_id,
        provider=account.provider,
        provider_type="oauth",  # TODO: This can most likely be removed look into it
        provider_account_id=account.provider_account_id,
        access_token=account.access_token,
        expires_at=datetime.datetime.fromtimestamp(account.expires_at),
    )
    return ""


@admin_router.delete("/account")
def unlink_account(account: AccountRequest):  # TODO, when would this be used?
    # Unlink an account from the user
    return {
        "message": f"Account {account.provider} unlinked for user {account.user_id}",
    }


### Not related to next-auth


def generate_key(size=32):
    buffer = secrets.token_bytes(size)
    key = base64.b64encode(buffer).decode("utf-8")
    # Replace forward slashes with hyphens to avoid issues with URL encoding
    return key.replace("/", "-")


## Tier-setting endpoint has been moved to orchestra/web/api/admin/views.py
## under generalized PUT /billing/tier.  Backward-compat alias PUT /user/tier
## is registered there.


@admin_router.put("/user/quotas/reset")
@admin_router.put("/auth-user/quotas/reset")  # backward-compat alias
def reset_user_quotas(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    user = user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    user_dao.update(id=user_id, queries_enabled=True, evaluations_enabled=True)
    return "User quotas reset successfully!"


@admin_router.put("/user/quotas/reset/all")
@admin_router.put("/auth-user/quotas/reset/all")  # backward-compat alias
def reset_all_user_quotas(
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    users = user_dao.filter()
    for user in users:
        user_dao.update(
            id=user[0].id,
            queries_enabled=True,
            evaluations_enabled=True,
        )
    return f"User quotas reset successfully for {len(users)} users"


@admin_router.get("/api_key/list")
def list_user_api_keys(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    api_key_dao = ApiKeyDAO(session)
    keys = api_key_dao.filter(user_id=user_id)
    if not keys:
        raise not_found("API Keys")
    return keys


@admin_router.post("/api_key")
def create_api_key(
    name: str,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
):
    api_key_dao = ApiKeyDAO(session)
    # TODO: This only allows for one api key at the time
    existing_api_key = api_key_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )
    if existing_api_key:
        raise HTTPException(
            status_code=400,
            detail="This user/organization already has an API key.",
        )
    new_api_key = generate_key()
    api_key_dao.create(
        key=new_api_key,
        name=name,
        user_id=user_id,
        organization_id=organization_id,
    )
    return new_api_key


@admin_router.post("/api_key/reset")
def reset_api_key(
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
):
    # TODO: This deletes all previous key from a user/org and creates a new one,
    # this will need to be changed once multiple keys are enabled.
    # delete prev key
    api_key_dao = ApiKeyDAO(session)
    old_api_key = api_key_dao.filter(user_id=user_id, organization_id=organization_id)
    api_key_dao.delete(id=old_api_key[0][0].id)
    new_api_key = generate_key()
    api_key_dao.create(
        key=new_api_key,
        name="",
        user_id=user_id,
        organization_id=organization_id,
    )
    return new_api_key


@admin_router.post("/api-keys/{key_id}/regenerate")
def regenerate_api_key(
    key_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Regenerate an API key by ID.

    Deletes the old key and creates a new one with the same metadata.
    Works for both personal and organization API keys.
    Returns the full new key.
    """
    api_key_dao = ApiKeyDAO(session)

    # Get existing key
    keys = api_key_dao.filter(id=key_id)
    if not keys:
        raise HTTPException(
            status_code=404,
            detail="API key not found",
        )

    old_key = keys[0][0]

    # Store metadata
    user_id = old_key.user_id
    organization_id = old_key.organization_id
    name = old_key.name

    # Delete old key
    api_key_dao.delete(key_id)

    # Create new key with same metadata
    new_api_key = generate_key()
    api_key_dao.create(
        key=new_api_key,
        name=name,
        user_id=user_id,
        organization_id=organization_id,
    )
    session.commit()

    return {
        "api_key": new_api_key,
        "user_id": user_id,
        "organization_id": organization_id,
    }


@admin_router.post("/user/{user_id}/organization-api-key")
@admin_router.post("/auth-user/{user_id}/organization-api-key")  # backward-compat alias
def create_organization_api_key(
    user_id: str,
    organization_id: int,
    name: str = "",
    custom_key: Optional[str] = None,
    session: Session = Depends(get_db_session),
):
    """
    Create an organization-specific API key for a user.

    This key will have organization context and billing will be charged to
    the organization's account.

    Args:
        user_id: The user ID to create the key for.
        organization_id: The organization ID.
        name: Optional name for the API key.
        custom_key: Optional custom API key value. If not provided, a random key
                    will be generated. Must be unique across all API keys.
    """
    api_key_dao = ApiKeyDAO(session)
    org_dao = OrganizationDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=404,
            detail=f"Organization with id {organization_id} not found",
        )

    # Verify user is a member of the organization
    memberships = org_member_dao.filter(
        organization_id=organization_id,
        user_id=user_id,
    )
    if not memberships:
        raise HTTPException(
            status_code=403,
            detail=f"User {user_id} is not a member of organization {organization_id}",
        )

    # Check if org API key already exists for this user+org
    existing_key = api_key_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )
    if existing_key:
        raise HTTPException(
            status_code=400,
            detail="User already has an organization API key for this organization",
        )

    # If custom key provided, verify it doesn't already exist
    if custom_key:
        existing_custom = api_key_dao.filter(key=custom_key)
        if existing_custom:
            raise HTTPException(
                status_code=400,
                detail="This API key value is already in use",
            )

    # Create organization API key (use custom key or generate one)
    new_api_key = custom_key or generate_key()
    api_key_dao.create(
        key=new_api_key,
        name=name or f"org_{org.name}",
        user_id=user_id,
        organization_id=organization_id,
    )
    session.commit()

    return {"api_key": new_api_key, "organization_id": organization_id}


@admin_router.get("/organization/list")
def list_organization(
    name: str,
    session: Session = Depends(get_db_session),
):
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)

    org = organization_dao.filter(name=name)
    if not org:
        raise not_found("Organization")
    org_members = organization_member_dao.list_members(name=name)
    return org_members


@admin_router.post("/organization")
def create_organization(
    name: str,
    owner_id: Optional[str] = None,
    session: Session = Depends(get_db_session),
):
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)
    user_dao = UserDAO(session)

    existing_org = organization_dao.filter(owner_id=owner_id)
    if existing_org:
        raise HTTPException(
            status_code=400,
            detail="This user already has an organization.",
        )

    # Get Owner role
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    if not owner_role:
        raise HTTPException(status_code=500, detail="Owner system role not found")

    # Get owner's timezone to initialize org timezone
    owner_row = user_dao.get_by_id(owner_id) if owner_id else None
    owner_timezone = owner_row[0].timezone if owner_row else None

    organization_dao.create(name=name, owner_id=owner_id, timezone=owner_timezone)
    new_org = organization_dao.filter(owner_id=owner_id)
    organization_member_dao.create(
        organization_id=new_org[0][0].id,
        user_id=owner_id,
        role_id=owner_role.id,
    )
    return "Organization created successfully!"


@admin_router.post("/organization/member")
def add_organization_member(
    name: str,
    new_member_email: str,
    role_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

    new_user = user_dao.filter(email=new_member_email)
    if not new_user:
        raise not_found("User")
    org = organization_dao.filter(name=name)
    if not org:
        raise not_found("Organization")

    # Default to Member role if not specified
    if role_id is None:
        member_role = role_dao.get_by_name("Member", organization_id=None)
        if not member_role:
            raise HTTPException(status_code=500, detail="Member system role not found")
        role_id = member_role.id

    organization_member_dao.create(
        organization_id=org[0][0].id,
        user_id=new_user[0][0].id,
        role_id=role_id,
    )

    # Grant Member access to Assistants project if it exists
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(session)
    assistants_projects = project_dao.filter(
        organization_id=org[0][0].id,
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
                grantee_id=new_user[0][0].id,
            )

    return "Member added successfully to the organization!"


@admin_router.put("/organization/member/role")
def update_organization_member_role(
    organization: str,
    member_email: str,
    role_id: int,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

    # Validate role exists
    role = role_dao.get(role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if not role.is_system_role:
        raise HTTPException(status_code=400, detail="Only system roles can be assigned")

    user = user_dao.filter(email=member_email)
    if not user:
        raise not_found("User")
    org = organization_dao.filter(name=organization)
    if not org:
        raise not_found("Organization")
    org_member = organization_member_dao.filter(
        user_id=user[0][0].id,
        organization_id=org[0][0].id,
    )
    if not org_member:
        raise not_found("Member")

    organization_member_dao.update_member_role(
        user_id=user[0][0].id,
        organization_id=org[0][0].id,
        role_id=role_id,
    )
    return f"Member role updated to {role.name}!"


@router.get("/user/query-logging")
def get_query_logging_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current query logging status for the authenticated user."""
    user_dao = UserDAO(session)
    user_id = request.state.user_id
    user = user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")

    return QueryLoggingStatus(enabled=user.queries_enabled)


@router.get("/user/basic-info")
def get_user_basic_info(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get basic information for the authenticated user."""
    user_dao = UserDAO(session)
    user_id = request.state.user_id
    user_row = user_dao.get_by_id(user_id)

    if not user_row:
        raise not_found("User")

    user = user_row[0]

    return {
        "user_id": user.id,
        "first": user.name,
        "last": user.last_name,
        "email": user.email,
        "job_title": user.job_title,
        "bio": user.bio,
        "timezone": user.timezone,
        "phone_number": user.phone_number,
    }


@router.patch("/user/query-logging")
def update_query_logging_status(
    request: Request,
    body: UpdateQueryLoggingRequest,
    session: Session = Depends(get_db_session),
):
    """Update the query logging status for the authenticated user."""
    user_dao = UserDAO(session)
    user_id = request.state.user_id
    user = user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")

    user_dao.update(id=user_id, queries_enabled=body.enabled)

    return QueryLoggingStatus(enabled=body.enabled)


# -- Manage one-time credit grant links --
@router.post(
    "/user/claim-credit-grant-link",
    response_model=CreditGrantClaimResponse,
    status_code=200,
)
@router.post(
    "/user/claim-assistant-hiring-one-time-link",  # backward-compat alias
    response_model=CreditGrantClaimResponse,
    status_code=200,
)
def claim_credit_grant_link(
    request: Request,
    payload: CreditGrantLinkClaimRequest,
    session: Session = Depends(get_db_session),
):
    """
    Claim a one-time credit grant link.

    When a user claims a valid link, they receive the credits specified in the link.
    Each user can only benefit from one link ever (checked via OneTimeCreditGrantLink table).

    Note: The approval-granting behavior has been removed. Access to assistant
    endpoints is now controlled by rate limits instead of approval status.
    """
    user_dao = UserDAO(session)
    user_id = request.state.user_id
    user_row_proxy = user_dao.get_by_id(user_id)
    if not user_row_proxy:
        raise not_found("User")

    user_instance = user_row_proxy[0]
    token_dao = OneTimeCreditGrantLinkDAO(session)

    link = token_dao.get_by_token(payload.token)
    if not link:
        raise not_found("Credit grant link token")

    # Check if user has already claimed any link before
    if token_dao.has_user_claimed_any_link(user_id):
        return CreditGrantClaimResponse(
            message="You have already benefited from a one-time credit grant link. "
            "This link was not consumed, and no new credits were awarded.",
            credits_granted=None,
        )

    # Check if link was already claimed by another user
    if link.user_id is not None:
        if link.user_id != user_instance.id:
            raise HTTPException(
                status_code=400,
                detail="This link has already been claimed by another user.",
            )
        # Edge case: same user, already claimed this specific link
        return CreditGrantClaimResponse(
            message="You already claimed this specific link.",
            credits_granted=None,
        )

    # Check if link has expired
    if link.expires_at < datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(status_code=400, detail="This link has expired.")

    try:
        # Claim the link
        claimed_link = token_dao.claim_link(payload.token, user_instance.id)
        if not claimed_link:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail="Failed to claim link. It may be invalid, expired, or "
                "already claimed by another user.",
            )

        # Grant credits to the user's billing account
        credit_amount = float(link.credit_amount)
        ba = user_instance.billing_account
        if ba:
            ba.credits += Decimal(str(credit_amount))
        else:
            from orchestra.db.dao.billing_account_dao import BillingAccountDAO

            ba_dao = BillingAccountDAO(session)
            new_ba = ba_dao.create(credits=Decimal(str(credit_amount)))
            user_instance.billing_account_id = new_ba.id
            session.flush()

        session.commit()
        return CreditGrantClaimResponse(
            message=f"Link successfully claimed! {credit_amount:.2f} credits awarded.",
            credits_granted=credit_amount,
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred: {str(e)}",
        )


@admin_router.post(
    "/credit-grant-link",
    response_model=CreditGrantLinkResponse,
    status_code=201,
)
@admin_router.post(
    "/assistant-hiring-one-time-link",  # backward-compat alias
    response_model=CreditGrantLinkResponse,
    status_code=201,
)
def create_credit_grant_link(
    payload: CreditGrantLinkCreateRequest,
    session: Session = Depends(get_db_session),
):
    """
    Create a one-time credit grant link.

    When a user claims this link, they receive the specified credit_amount.
    If credit_amount is not provided, defaults to assistant_creation_cost.
    """
    token_dao = OneTimeCreditGrantLinkDAO(session)
    if payload.expires_in_days <= 0:
        raise HTTPException(status_code=400, detail="Expiration days must be positive.")

    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=payload.expires_in_days,
    )
    link = token_dao.create(
        expires_at=expires_at,
        credit_amount=payload.credit_amount,
    )
    session.commit()
    session.refresh(link)
    return CreditGrantLinkResponse(
        id=link.id,
        token=link.token,
        expires_at=link.expires_at,
        claimed_at=link.claimed_at,
        user_id=link.user_id,
        credit_amount=link.credit_amount,
    )


@admin_router.get(
    "/credit-grant-link",
    response_model=List[CreditGrantLinkResponse],
)
@admin_router.get(
    "/assistant-hiring-one-time-link",  # backward-compat alias
    response_model=List[CreditGrantLinkResponse],
)
def list_credit_grant_links(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    """List all one-time credit grant links."""
    token_dao = OneTimeCreditGrantLinkDAO(session)
    links = token_dao.list_links(limit=limit, offset=offset)
    return [
        CreditGrantLinkResponse(
            id=link.id,
            token=link.token,
            expires_at=link.expires_at,
            claimed_at=link.claimed_at,
            user_id=link.user_id,
            credit_amount=link.credit_amount,
        )
        for link in links
    ]


@admin_router.delete("/credit-grant-link/{link_id}", status_code=204)
@admin_router.delete(
    "/assistant-hiring-one-time-link/{link_id}",
    status_code=204,
)  # backward-compat alias
def delete_credit_grant_link(
    link_id: str,
    session: Session = Depends(get_db_session),
):
    token_dao = OneTimeCreditGrantLinkDAO(session)
    if not token_dao.delete_link(link_id):
        raise not_found("One-time credit grant link")
    session.commit()
    return None


@router.post("/billing/validate-tax-id")
@router.post("/user/validate-tax-id")  # backward-compat alias
def validate_tax_id(
    request: Request,
    tax_id: str = Query(..., description="Tax ID to validate"),
    country: str = Query(..., description="Two-letter country code"),
    session: Session = Depends(get_db_session),
):
    """Validate a tax ID format for a specific country."""
    try:
        validation_result = validate_tax_id_for_country(tax_id, country)

        return {
            "tax_id": tax_id,
            "country": country.upper(),
            "is_valid": validation_result["is_valid"],
            "formatted_tax_id": validation_result["formatted_tax_id"],
            "error": validation_result["error"],
            "supported_countries": TaxIDValidator.get_supported_countries(),
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")


@router.get("/billing/supported-tax-countries")
@router.get("/user/supported-tax-countries")  # backward-compat alias
def get_supported_tax_countries():
    """Get list of countries supported for tax ID validation."""
    return {
        "supported_countries": TaxIDValidator.get_supported_countries(),
        "total_countries": len(TaxIDValidator.get_supported_countries()),
    }


@router.get("/user/onboarding-status", response_model=OnboardingStatusResponse)
def get_onboarding_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current user's onboarding status."""
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    user = user_row[0]
    return OnboardingStatusResponse(onboarded=user.onboarded)


@router.put("/user/onboarding-status")
def update_onboarding_status(
    request: Request,
    body: UpdateOnboardingStatusRequest,
    session: Session = Depends(get_db_session),
):
    """Update the current user's onboarding status."""
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    user_dao.update(id=request.state.user_id, onboarded=body.onboarded)
    session.commit()

    return {"message": "Onboarding status updated successfully"}


# -- Detailed Onboarding Progress (Step-by-Step) --


@router.get("/user/onboarding", response_model=OnboardingStatusDetailedResponse)
def get_onboarding_progress(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Get the current user's detailed onboarding progress.

    Returns the current step and step-specific data that can be used
    to resume onboarding from where the user left off.
    """
    user_dao = UserDAO(session)
    onboarding_dao = OnboardingStatusDAO(session)

    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    # Get or create onboarding status
    status = onboarding_dao.get_or_create(request.state.user_id)
    session.commit()

    return OnboardingStatusDetailedResponse(
        user_id=status.user_id,
        current_step=status.current_step,
        step_data=OnboardingStepDataResponse(**(status.step_data or {})),
        created_at=status.created_at,
        updated_at=status.updated_at,
    )


@router.put("/user/onboarding", response_model=OnboardingStatusDetailedResponse)
def update_onboarding_progress(
    request: Request,
    body: OnboardingStatusUpdateRequest,
    session: Session = Depends(get_db_session),
):
    """
    Update the current user's onboarding progress.

    The step_data is validated based on the current_step to ensure
    only valid fields are stored.
    """
    user_dao = UserDAO(session)
    onboarding_dao = OnboardingStatusDAO(session)

    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    # Get or create, then update
    status = onboarding_dao.get_or_create(request.state.user_id)
    status = onboarding_dao.update(
        user_id=request.state.user_id,
        current_step=body.current_step,
        step_data=body.step_data,
    )

    # If step is "completed", also set the legacy onboarded flag
    if body.current_step == "completed":
        user_dao.update(id=request.state.user_id, onboarded=True)

    session.commit()

    return OnboardingStatusDetailedResponse(
        user_id=status.user_id,
        current_step=status.current_step,
        step_data=OnboardingStepDataResponse(**(status.step_data or {})),
        created_at=status.created_at,
        updated_at=status.updated_at,
    )


@router.delete("/user/onboarding")
def reset_onboarding_progress(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Reset the current user's onboarding progress.

    This can be used if the user wants to restart the onboarding flow.
    """
    user_dao = UserDAO(session)
    onboarding_dao = OnboardingStatusDAO(session)

    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    status = onboarding_dao.reset(request.state.user_id)

    # Also reset the legacy onboarded flag
    user_dao.update(id=request.state.user_id, onboarded=False)

    session.commit()

    return {
        "message": "Onboarding progress reset successfully",
        "current_step": status.current_step,
    }


# -- Account Deletion (Self-Service) --


@router.get("/user/can-delete-account", response_model=CanDeleteAccountResponse)
def can_delete_account(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Pre-flight check for account deletion.

    Returns whether the current user can delete their account,
    and if not, the reasons why (pending bills, org ownership, etc.).
    """
    cleanup_service = UserAccountCleanupService(session)
    blockers = cleanup_service.check_deletion_blockers(request.state.user_id)

    blocker_responses = [
        DeletionBlockerResponse(reason=b.reason, details=b.details) for b in blockers
    ]

    return CanDeleteAccountResponse(
        can_delete=len(blockers) == 0,
        blockers=blocker_responses,
    )


@router.delete("/user/delete-account", response_model=AccountDeletionResponse)
def delete_own_account(
    request: Request,
    body: AccountDeletionConfirmation,
    session: Session = Depends(get_db_session),
):
    """
    Delete the current user's account (self-service).

    Requires email confirmation to prevent accidental deletion.
    Permanently removes all user data - this action cannot be undone.

    Blocked if:
    - User has pending bills
    - User owns organizations (must transfer ownership first)
    """
    user_dao = UserDAO(session)
    user = user_dao.get_by_id(request.state.user_id)

    if not user:
        raise not_found("User")

    if user[0].email.lower() != body.confirm_email.lower():
        raise HTTPException(
            status_code=400,
            detail="Email confirmation does not match account email",
        )

    cleanup_service = UserAccountCleanupService(session)
    result = cleanup_service.delete_user_account(request.state.user_id)

    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)

    return AccountDeletionResponse(success=True, message=result.message)


# ============================================================================
# User Spending Limit Endpoints (Personal Context)
# ============================================================================


@router.put("/user/spending-limit", response_model=UserSpendingLimitResponse)
async def set_user_spending_limit(
    request: Request,
    body: UserSpendingLimitRequest,
    session: Session = Depends(get_db_session),
) -> UserSpendingLimitResponse:
    """
    Set the monthly spending limit for the current user's personal usage.

    This limit applies when using the user's personal API key (not org API keys).
    When the limit is lowered, personal assistant limits that exceed the new limit
    will be automatically capped.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)

    # Verify user exists
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise not_found("User")

    # Use the DAO method which handles cascade logic
    cascade_result = user_dao.set_spending_cap(
        user_id=user_id,
        monthly_spending_cap=body.monthly_spending_cap,
    )
    session.commit()

    return UserSpendingLimitResponse(
        user_id=user_id,
        monthly_spending_cap=body.monthly_spending_cap,
        assistants_capped=cascade_result.assistants_capped,
    )


@router.get("/user/spending-limit", response_model=UserSpendingLimitResponse)
async def get_user_spending_limit(
    request: Request,
    session: Session = Depends(get_db_session),
) -> UserSpendingLimitResponse:
    """
    Get the monthly spending limit for the current user's personal usage.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)

    # Verify user exists
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise not_found("User")

    # Use DAO method for consistency
    spending_cap = user_dao.get_spending_cap(user_id)

    return UserSpendingLimitResponse(
        user_id=user_id,
        monthly_spending_cap=spending_cap,
        assistants_capped=0,
    )


# ============================================================================
# User Billing / Checkout Endpoints
# ============================================================================


@router.post(
    "/user/billing/checkout",
    response_model=UserCheckoutResponse,
    responses={
        200: {
            "description": "Checkout session created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "checkout_url": "https://checkout.stripe.com/...",
                        "session_id": "cs_test_...",
                    },
                },
            },
        },
        400: {
            "description": "Invalid request",
        },
        500: {
            "description": "Failed to create checkout session",
        },
    },
)
async def create_user_checkout_session(
    request_fastapi: Request,
    checkout_request: UserCheckoutRequest,
    session: Session = Depends(get_db_session),
) -> UserCheckoutResponse:
    """
    Create a Stripe checkout session for purchasing credits for the current user.

    This endpoint creates a one-time payment checkout session for the
    authenticated user's personal workspace. Upon successful payment,
    credits will be added to the user's balance via webhook.

    If the user doesn't have a Stripe customer ID yet, one will be
    created during the checkout process.

    Args:
        amount: Amount of credits to purchase (1 credit = $1, min 5, max 10000)
        success_url: URL to redirect to on successful payment
        cancel_url: URL to redirect to on cancelled payment

    Returns:
        - checkout_url: URL to redirect the user to for payment
        - session_id: Stripe checkout session ID
    """
    import stripe

    user_id = request_fastapi.state.user_id
    user_dao = UserDAO(session)

    # Get user
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user = user_row[0]

    # Configure Stripe
    if not settings.stripe_secret_key:
        logger.error("Stripe secret key not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment system not configured",
        )

    stripe.api_key = settings.stripe_secret_key

    try:
        # Ensure user has a BillingAccount and Stripe customer (lazy creation)
        billing_account_dao = BillingAccountDAO(session)
        ba = user.billing_account
        if ba is None:
            ba = billing_account_dao.create()
            user.billing_account_id = ba.id
            session.flush()

        if not ba.stripe_customer_id:
            from orchestra.web.api.utils.business_validation import (
                build_stripe_customer_name,
                get_stripe_tax_exempt_status,
                get_stripe_tax_id_data,
            )

            customer_params = {
                "email": user.email,
                "metadata": {
                    "user_id": user_id,
                    "billing_account_id": str(ba.id),
                },
            }

            # Include profile name — users are always individuals
            if ba.name:
                customer_params.update(
                    build_stripe_customer_name(
                        is_business=False,
                        name=ba.name,
                    ),
                )
            if ba.billing_email:
                customer_params["email"] = ba.billing_email

            # Include address if available
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
                customer_params["tax"] = {"validate_location": "immediately"}

            # Include tax ID if available
            country_code = ba_address.get("country")
            tax_id_data = get_stripe_tax_id_data(ba.tax_id, country_code)
            if tax_id_data:
                customer_params["tax_id_data"] = tax_id_data

            customer_params["tax_exempt"] = get_stripe_tax_exempt_status(
                ba.tax_id,
                country_code,
            )

            customer = stripe.Customer.create(**customer_params)
            ba.stripe_customer_id = customer.id
            session.flush()
            logger.info(
                {
                    "message": "Created Stripe customer for user",
                    "user_id": user_id,
                    "stripe_customer_id": customer.id,
                },
            )

        # Resolve the pre-configured Price ID (personal context for this endpoint)
        price_id = settings.stripe_unify_credits_price_id_personal
        if not price_id:
            logger.error("STRIPE_UNIFY_CREDITS_PRICE_ID_PERSONAL not configured")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Payment system not fully configured",
            )

        # Build checkout session parameters
        checkout_params = {
            "mode": "payment",
            "submit_type": "pay",
            "customer": ba.stripe_customer_id,
            "client_reference_id": user_id,
            "line_items": [
                {
                    "price": price_id,
                    "quantity": checkout_request.amount,
                },
            ],
            "automatic_tax": {"enabled": True},
            "customer_update": {
                "address": "auto",
                "name": "auto",
            },
            "billing_address_collection": "required",
            "tax_id_collection": {"enabled": True},
            "success_url": checkout_request.success_url,
            "cancel_url": checkout_request.cancel_url,
            "metadata": {
                "user_id": user_id,
                "credits_purchased": str(checkout_request.amount),
            },
            "payment_intent_data": {
                "metadata": {
                    "user_id": user_id,
                    "credits_purchased": str(checkout_request.amount),
                },
            },
            "payment_method_options": {
                "card": {"request_three_d_secure": "any"},
            },
            "invoice_creation": {
                "enabled": True,
                "invoice_data": {
                    "description": f"Unify Credits purchase ({checkout_request.amount} credits)",
                },
            },
        }

        checkout_session = stripe.checkout.Session.create(**checkout_params)

        if not checkout_session.url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create checkout session URL",
            )

        logger.info(
            {
                "message": "User checkout session created",
                "user_id": user_id,
                "amount": checkout_request.amount,
                "session_id": checkout_session.id,
            },
        )

        return UserCheckoutResponse(
            checkout_url=checkout_session.url,
            session_id=checkout_session.id,
        )

    except stripe.error.StripeError as e:
        logger.error(
            {
                "message": "Failed to create checkout session",
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create checkout session: {str(e)}",
        )


# ============================================================================
# User Business Profile Endpoints
# ============================================================================


@router.get(
    "/user/billing/billing-profile",
    response_model=UserBillingProfileResponse,
    summary="Get user business profile",
    description="Get the current user's billing/business profile information.",
)
def get_user_billing_profile(
    request: Request,
    session: Session = Depends(get_db_session),
) -> UserBillingProfileResponse:
    """
    Get the current user's billing profile.

    Returns billing_email, individual_name (+ business_name alias),
    tax_id, tax_id_type, billing_address from the user's BillingAccount.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user = user_dao.get_user_with_id(user_id)

    ba = user.billing_account
    if not ba:
        return UserBillingProfileResponse()

    billing_account_dao = BillingAccountDAO(session)
    profile = billing_account_dao.get_billing_profile(ba.id)
    if not profile:
        return UserBillingProfileResponse()

    # Map DAO's generic "name" to individual_name + backward-compat alias
    name = profile.pop("name", None)
    profile["individual_name"] = name
    profile["business_name"] = name  # backward-compat alias
    return UserBillingProfileResponse(**profile)


@router.patch(
    "/user/billing/billing-profile",
    response_model=UserBillingProfileResponse,
    summary="Update user business profile",
    description="Update the current user's billing/business profile information.",
)
def update_user_billing_profile(
    request: Request,
    profile_update: UserBillingProfileUpdate,
    session: Session = Depends(get_db_session),
) -> UserBillingProfileResponse:
    """
    Update the current user's business profile (billing details).

    Only provided fields are updated. Billing address is merged with existing data.
    Also syncs changes to Stripe customer if one exists.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user = user_dao.get_user_with_id(user_id)

    ba = user.billing_account
    if not ba:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no billing account",
        )

    resolved_name = profile_update.resolved_name

    # Validate billing address if provided
    if profile_update.billing_address is not None:
        from orchestra.web.api.utils.business_validation import (
            validate_billing_address_data,
        )

        addr = (
            profile_update.billing_address
            if isinstance(profile_update.billing_address, dict)
            else {}
        )
        if addr.get("line1") or addr.get("city") or addr.get("country"):
            is_valid, error_msg = validate_billing_address_data(
                line1=addr.get("line1"),
                city=addr.get("city"),
                country=addr.get("country"),
                line2=addr.get("line2"),
                state=addr.get("state"),
                postal_code=addr.get("postal_code"),
            )
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid billing address: {error_msg}",
                )

    billing_account_dao = BillingAccountDAO(session)
    billing_account_dao.update_billing_profile(
        billing_account_id=ba.id,
        billing_email=profile_update.billing_email,
        name=resolved_name,
        tax_id=profile_update.tax_id,
        tax_id_type=profile_update.tax_id_type,
        billing_address=profile_update.billing_address,
    )
    session.flush()

    # Sync to Stripe if customer exists
    if ba.stripe_customer_id:
        try:
            import stripe

            from orchestra.web.api.utils.business_validation import (
                build_stripe_customer_name,
                sync_tax_id_to_stripe,
            )

            stripe.api_key = settings.stripe_secret_key

            update_params: dict = {}
            if profile_update.billing_email is not None:
                update_params["email"] = profile_update.billing_email
            if resolved_name is not None:
                # Users are individuals; pass is_business=False so Stripe
                # gets individual_name.  If the user supplied a tax_id
                # they're treated as a business for tax purposes, but
                # the *name* is still their individual name.
                update_params.update(
                    build_stripe_customer_name(
                        is_business=False,
                        name=resolved_name,
                    ),
                )

            # Sync billing address to Stripe
            billing_address_dict = None
            if profile_update.billing_address is not None:
                billing_address_dict = (
                    profile_update.billing_address
                    if isinstance(profile_update.billing_address, dict)
                    else profile_update.billing_address
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
                stripe.Customer.modify(ba.stripe_customer_id, **update_params)

            # Sync tax ID if provided (requires separate API calls)
            if profile_update.tax_id is not None:
                country_code = None
                if billing_address_dict and billing_address_dict.get("country"):
                    country_code = billing_address_dict["country"]
                elif ba.billing_address and ba.billing_address.get("country"):
                    country_code = ba.billing_address["country"]

                sync_tax_id_to_stripe(
                    ba.stripe_customer_id,
                    profile_update.tax_id,
                    country_code,
                    logger=logger,
                )
        except Exception as e:
            logging.warning(
                f"Failed to sync business profile to Stripe for user {user_id}: {e}",
            )

    session.commit()

    profile = billing_account_dao.get_billing_profile(ba.id)
    name = profile.pop("name", None)
    profile["individual_name"] = name
    profile["business_name"] = name  # backward-compat alias
    return UserBillingProfileResponse(**profile)


# ============================================================================
# Admin Spend Endpoints (for UniLLM service calls)
# ============================================================================


@admin_router.get("/user/{target_user_id}/spend", response_model=UserSpendResponse)
def admin_get_user_spend(
    target_user_id: str,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
) -> UserSpendResponse:
    """
    Admin endpoint: Get a user's cumulative spend for a given month (personal context).

    This endpoint is for internal service calls (e.g., UniLLM) and does not
    require the caller to be the user themselves.
    """
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(target_user_id)
    if not user_row:
        raise not_found("User")

    user = user_row[0]

    cumulative_spend = user_dao.get_cumulative_spend(target_user_id, month)
    limit = user_dao.get_spending_cap(target_user_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    return UserSpendResponse(
        user_id=target_user_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=user.monthly_spending_cap_set_at,
        percent_used=percent_used,
    )


# ============================================================================
# Backward-Compat Stub Endpoints
# ============================================================================
# These stubs preserve the old API surface so that external repos (console,
# ivory, etc.) continue to work after the underlying models and logic have
# been refactored.  They should be removed once all callers have migrated.
# ============================================================================


# -- Assistant Hiring Approval stubs (approval flow removed; access now
#    controlled by rate limits) --


@router.post("/user/assistant-hiring-approval")
def _compat_request_assistant_hiring_approval(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: always returns 'approved' (approval flow removed)."""
    return {
        "message": "Access is now managed through rate limits. No approval required.",
        "assistant_hiring_approval": "approved",
    }


@admin_router.put(
    "/auth-user/{target_user_id}/assistant-hiring-approval/{status_value}",
)
def _compat_set_user_assistant_hiring_status(
    target_user_id: str,
    status_value: str,
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (approval flow removed)."""
    return {
        "message": (
            f"User {target_user_id} assistant hiring approval status "
            f"set to '{status_value}' (no-op — approval flow removed)."
        ),
        "assistant_hiring_approval": status_value,
    }


@admin_router.get("/auth-user/assistant-hiring-approval")
def _compat_list_users_by_assistant_hiring_approval(
    status_filter: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: returns empty list (approval flow removed)."""
    return []


# -- Old Business / Account-Type stubs (replaced by BillingAccount-based
#    business profile endpoints) --


@router.get("/user/business-status")
def _compat_get_user_business_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: returns business profile data mapped to old schema."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    user = user_row[0]
    ba = user.billing_account

    # Map new BillingAccount fields to old response shape
    billing_address = None
    if ba and ba.billing_address:
        addr = ba.billing_address if isinstance(ba.billing_address, dict) else {}
        billing_address = {
            "line1": addr.get("line1", ""),
            "line2": addr.get("line2", ""),
            "city": addr.get("city", ""),
            "state": addr.get("state", ""),
            "country": addr.get("country", ""),
            "postal_code": addr.get("postal_code", ""),
        }

    return {
        "account_type": "individual",  # field removed; default
        "individual_name": ba.name if ba else None,
        "business_name": ba.name if ba else None,  # backward-compat alias
        "tax_id": ba.tax_id if ba else None,
        "business_type": None,  # field removed
        "business_verified": False,  # field removed; default
        "tax_exempt": False,  # field removed; default
        "tax_jurisdiction": None,  # field removed
        "business_address": billing_address,
    }


@router.put("/user/account-type")
def _compat_update_user_account_type(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (account-type concept removed)."""
    return {"message": "Account type updated successfully (no-op — field removed)"}


@router.patch("/user/business-info")
def _compat_update_user_business_info(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (use PATCH /user/billing/billing-profile instead)."""
    return {
        "message": "Business information updated successfully (no-op — use /user/billing/billing-profile)",
    }


@admin_router.post("/auth-user/verify-business")
def _compat_verify_business_account(
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (old verify-business flow removed)."""
    return {"message": "Business account verification is no longer required (no-op)."}


@admin_router.get("/auth-user/business-accounts")
def _compat_list_business_accounts(
    verified: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: returns empty list (old business-accounts listing removed)."""
    return []


@router.post("/user/create-with-business-info")
def _compat_create_user_with_business_info(
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (use POST /user with standard flow instead)."""
    return {"message": "Use the standard user creation flow (POST /admin/user)."}
