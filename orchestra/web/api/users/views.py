import asyncio
import base64
import datetime
import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.assistant_hiring_one_time_approval_link_dao import (
    AssistantHiringOneTimeApprovalLinkDAO,
)
from orchestra.db.dao.auth_user_dao import (
    ASSISTANT_HIRING_APPROVAL_STATUSES,
    AuthUser,
    AuthUserDAO,
)
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.settings import settings
from orchestra.web.api.users.schema import (
    AccountRequest,
    AssistantHiringApprovalCreateLinkRequest,
    AssistantHiringApprovalResponse,
    AssistantHiringApprovalUserStatus,
    AssistantHiringOneTimeLinkClaimTokenRequest,
    AssistantHiringOneTimeLinkResponse,
    BusinessAddress,
    BusinessVerificationRequest,
    FreezeAccountByStripeIdRequest,
    FreezeAccountRequest,
    OnboardingStatusResponse,
    QueryLoggingStatus,
    StripeIdRequest,
    UpdateAccountTypeRequest,
    UpdateBusinessInfoRequest,
    UpdateOnboardingStatusRequest,
    UpdateQueryLoggingRequest,
    UserBusinessStatusResponse,
    UserRequest,
)
from orchestra.web.api.utils.business_validation import (
    format_business_address,
    format_business_classification,
)
from orchestra.web.api.utils.email import send_email_async
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


@admin_router.post("/auth-user")
async def create_user(
    user: UserRequest,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    user_dao = UsersDAO(session)

    auth_user_dao.create(
        email=user.email,
        name=user.name,
        last_name=user.last_name,
        job_title=user.job_title,
        bio=user.bio,
        image=user.image,
        timezone=user.timezone,
    )
    user_row = auth_user_dao.filter(email=user.email)
    new_user = user_row[0][0]

    new_api_key = generate_key()
    api_key_dao.create(key=new_api_key, name="", user_id=new_user.id)
    # TODO: remove this after migrating
    try:
        user_dao.create_users(id=new_user.id, credits=0)
        # Seed default Unity project, interface, tab, and table tile for tasks
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
    }


@admin_router.get("/auth-user/by-user-id")
async def get_user(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]

    org_member = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_role_id, org_role_name = None, None, None
    if org_member:
        org_role_id = org_member[0][0].role_id
        role = role_dao.get(org_role_id)
        org_role_name = role.name if role else None
        org = organization_dao.filter(id=org_member[0][0].organization_id)
        org_name = org[0][0].name
    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "lastName": user_instance.last_name,
        "jobTitle": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "createdAt": user_instance.created_at,
        "apiKey": api_key_instance.key,
        "organization": {
            "name": org_name,
            "role_id": org_role_id,
            "role_name": org_role_name,
        },
        "assistant_hiring_approval": user_instance.assistant_hiring_approval,
        "has_claimed_approval_link": user_instance.has_claimed_approval_link,
        "business_classification": format_business_classification(user_instance),
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
    }


@admin_router.get("/auth-user/by-email")
async def get_user_by_email(
    email: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    user = auth_user_dao.filter(email=email)
    if not user:
        return None
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]

    org_members = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_role_id, org_role_name = None, None, None
    if org_members:
        org_role_id = org_members[0][0].role_id
        role = role_dao.get(org_role_id)
        org_role_name = role.name if role else None
        org = organization_dao.filter(id=org_members[0][0].organization_id)
        org_name = org[0][0].name

    # Build organizations list with org-specific API keys
    organizations = []
    for member_row in org_members:
        member = member_row[0]
        org_result = organization_dao.get(member.organization_id)
        if org_result:
            # Get org-specific API key for this user+org
            org_keys = api_key_dao.get_organization_keys(
                user_instance.id,
                organization_id=member.organization_id,
            )
            org_api_key = org_keys[0][0].key if org_keys else None
            # Get role name for this membership
            member_role = role_dao.get(member.role_id)
            member_role_name = member_role.name if member_role else None
            organizations.append(
                {
                    "id": member.organization_id,
                    "name": org_result.name,
                    "role_id": member.role_id,
                    "role_name": member_role_name,
                    "apiKey": org_api_key,
                },
            )

    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "lastName": user_instance.last_name,
        "jobTitle": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "createdAt": user_instance.created_at,
        "apiKey": api_key_instance.key,
        "organization": {
            "name": org_name,
            "role_id": org_role_id,
            "role_name": org_role_name,
        },
        "organizations": organizations,
        "assistant_hiring_approval": user_instance.assistant_hiring_approval,
        "has_claimed_approval_link": user_instance.has_claimed_approval_link,
        "business_classification": format_business_classification(user_instance),
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
    }


@admin_router.get("/auth-user/by-account")
async def get_user_by_account(
    provider_account_id: str,
    provider: str,
    session: Session = Depends(get_db_session),
):
    account_dao = AccountDAO(session)
    auth_user_dao = AuthUserDAO(session)
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
    user = auth_user_dao.filter(id=account[0][0].user_id)
    if not user:
        return None
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user[0][0].id)
    api_key_instance = api_key[0][0]

    org_member = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_role_id, org_role_name = None, None, None
    if org_member:
        org_role_id = org_member[0][0].role_id
        role = role_dao.get(org_role_id)
        org_role_name = role.name if role else None
        org = organization_dao.filter(id=org_member[0][0].organization_id)
        org_name = org[0][0].name
    return {
        "id": user_instance.id,
        "name": user_instance.name,
        "lastName": user_instance.last_name,
        "jobTitle": user_instance.job_title,
        "bio": user_instance.bio,
        "image": user_instance.image,
        "email": user_instance.email,
        "createdAt": user_instance.created_at,
        "apiKey": api_key_instance.key,
        "organization": {
            "name": org_name,
            "role_id": org_role_id,
            "role_name": org_role_name,
        },
        "assistant_hiring_approval": user_instance.assistant_hiring_approval,
        "has_claimed_approval_link": user_instance.has_claimed_approval_link,
        "business_classification": format_business_classification(user_instance),
        "onboarded": user_instance.onboarded,
        "timezone": user_instance.timezone,
    }


@admin_router.put("/auth-user")
async def update_user(
    updated_user: UserRequest,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    user = auth_user_dao.filter(id=updated_user.user_id)
    if not user:
        raise not_found("User")
    auth_user_dao.update(
        id=updated_user.user_id,
        name=updated_user.name,
        last_name=updated_user.last_name,
        job_title=updated_user.job_title,
        bio=updated_user.bio,
        timezone=updated_user.timezone,
    )
    return "User information updated successfully!"


@admin_router.delete("/auth-user")
async def delete_user(user_id: str, session: Session = Depends(get_db_session)):
    auth_user_dao = AuthUserDAO(session)
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise not_found("User")
    auth_user_dao.delete(id=user_id)
    return "User deleted successfully!"


@admin_router.post("/account")
async def link_account(
    account: AccountRequest,
    session: Session = Depends(get_db_session),
):
    account_dao = AccountDAO(session)
    account_dao.create(
        user_id=account.userId,
        provider=account.provider,
        provider_type="oauth",  # TODO: This can most likely be removed look into it
        provider_account_id=account.providerAccountId,
        access_token=account.access_token,
        expires_at=datetime.datetime.fromtimestamp(account.expires_at),
    )
    return ""


@admin_router.delete("/account")
async def unlink_account(account: AccountRequest):  # TODO, when would this be used?
    # Unlink an account from the user
    return {"message": f"Account {account.provider} unlinked for user {account.userId}"}


### Not related to next-auth


def generate_key(size=32):
    buffer = secrets.token_bytes(size)
    key = base64.b64encode(buffer).decode("utf-8")
    # Replace forward slashes with hyphens to avoid issues with URL encoding
    return key.replace("/", "-")


@admin_router.put("/auth-user/tier")
async def set_user_tier(
    user_id: str,
    tier: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    if tier not in ["developer", "professional", "enterprise"]:
        raise HTTPException(
            status_code=400,
            detail="Tier must be one of developer, professional, or enterprise.",
        )
    auth_user_dao.update(id=user_id, tier=tier)
    return "User tier updated successfully!"


@admin_router.put("/auth-user/quotas/reset")
async def reset_user_quotas(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    auth_user_dao.update(id=user_id, queries_enabled=True, evaluations_enabled=True)
    return "User quotas reset successfully!"


@admin_router.put("/auth-user/quotas/reset/all")
async def reset_all_user_quotas(
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    users = auth_user_dao.filter()
    for user in users:
        auth_user_dao.update(
            id=user[0].id,
            queries_enabled=True,
            evaluations_enabled=True,
        )
    return f"User quotas reset successfully for {len(users)} users"


@admin_router.post("/auth-user/freeze")
async def freeze_account(
    request: FreezeAccountRequest,
    session: Session = Depends(get_db_session),
):
    users_dao = UsersDAO(session)
    users_dao.set_frozen_status(request.user_id, request.freeze)
    status_str = "frozen" if request.freeze else "unfrozen"
    return {"message": f"Account {status_str} successfully!"}


@admin_router.post("/auth-user/freeze-by-stripe-id")
async def freeze_account_by_stripe_id(
    request: FreezeAccountByStripeIdRequest,
    session: Session = Depends(get_db_session),
):
    users_dao = UsersDAO(session)
    user = users_dao.get_user_by_stripe_id(request.stripe_id)
    if not user:
        raise not_found("User with specified Stripe ID")
    users_dao.set_frozen_status(user.id, request.freeze)
    status_str = "frozen" if request.freeze else "unfrozen"
    return {
        "message": f"Account with stripe_id {request.stripe_id} {status_str} successfully!",
    }


@admin_router.put("/auth-user/stripe-id")
async def set_stripe_id_for_user(
    request: StripeIdRequest,
    session: Session = Depends(get_db_session),
):
    users_dao = UsersDAO(session)
    users_dao.set_stripe_customer_id(request.user_id, request.stripe_id)
    session.commit()
    return {"message": f"Stripe ID set for user {request.user_id}"}


@admin_router.get("/auth-user/is-frozen")
async def is_account_frozen(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    users_dao = UsersDAO(session)
    frozen = users_dao.is_account_frozen(user_id)
    return {"user_id": user_id, "is_frozen": frozen}


@admin_router.get("/api_key/list")
async def list_user_api_keys(
    user_id: str,
    session: Session = Depends(get_db_session),
):
    api_key_dao = ApiKeyDAO(session)
    keys = api_key_dao.filter(user_id=user_id)
    if not keys:
        raise not_found("API Keys")
    return keys


@admin_router.post("/api_key")
async def create_api_key(
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
async def reset_api_key(
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
async def regenerate_api_key(
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


@admin_router.post("/auth-user/{user_id}/organization-api-key")
async def create_organization_api_key(
    user_id: str,
    organization_id: int,
    name: str = "",
    session: Session = Depends(get_db_session),
):
    """
    Create an organization-specific API key for a user.

    This key will have organization context and billing will be charged to
    the organization's billing_user_id.
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

    # Check if org API key already exists
    existing_key = api_key_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )
    if existing_key:
        raise HTTPException(
            status_code=400,
            detail="User already has an organization API key for this organization",
        )

    # Create organization API key
    new_api_key = generate_key()
    api_key_dao.create(
        key=new_api_key,
        name=name or f"org_{org.name}",
        user_id=user_id,
        organization_id=organization_id,
    )
    session.commit()

    return {"api_key": new_api_key, "organization_id": organization_id}


@admin_router.get("/organization/list")
async def list_organization(
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
async def create_organization(
    name: str,
    owner_id: Optional[str] = None,
    session: Session = Depends(get_db_session),
):
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

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

    organization_dao.create(name=name, owner_id=owner_id)
    new_org = organization_dao.filter(owner_id=owner_id)
    organization_member_dao.create(
        organization_id=new_org[0][0].id,
        user_id=owner_id,
        role_id=owner_role.id,
    )
    return "Organization created successfully!"


@admin_router.post("/organization/member")
async def add_organization_member(
    name: str,
    new_member_email: str,
    role_id: Optional[int] = None,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

    new_user = auth_user_dao.filter(email=new_member_email)
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
    return "Member added successfully to the organization!"


@admin_router.put("/organization/member/role")
async def update_organization_member_role(
    organization: str,
    member_email: str,
    role_id: int,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    role_dao = RoleDAO(session)

    # Validate role exists
    role = role_dao.get(role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if not role.is_system_role:
        raise HTTPException(status_code=400, detail="Only system roles can be assigned")

    user = auth_user_dao.filter(email=member_email)
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
async def get_query_logging_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current query logging status for the authenticated user."""
    auth_user_dao = AuthUserDAO(session)
    user_id = request.state.user_id
    user = auth_user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")

    return QueryLoggingStatus(enabled=user.queries_enabled)


@router.get("/user/basic-info")
async def get_user_basic_info(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get basic information for the authenticated user."""
    auth_user_dao = AuthUserDAO(session)
    user_id = request.state.user_id
    user_row = auth_user_dao.get_by_id(user_id)

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
    }


@router.patch("/user/query-logging")
async def update_query_logging_status(
    request: Request,
    body: UpdateQueryLoggingRequest,
    session: Session = Depends(get_db_session),
):
    """Update the query logging status for the authenticated user."""
    auth_user_dao = AuthUserDAO(session)
    user_id = request.state.user_id
    user = auth_user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")

    auth_user_dao.update(id=user_id, queries_enabled=body.enabled)

    return QueryLoggingStatus(enabled=body.enabled)


# -- Business Classification Endpoints --


@router.get("/user/business-status", response_model=UserBusinessStatusResponse)
async def get_user_business_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current user's business classification status."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    auth_user_dao = AuthUserDAO(session)
    user_row = auth_user_dao.get_by_id(user_id)

    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    auth_user = user_row[0]

    # Use standardized business validation
    business_address = format_business_address(auth_user)

    # Convert dict to BusinessAddress object if address exists
    business_address_obj = None
    if business_address:
        business_address_obj = BusinessAddress(
            address_line1=business_address["address_line1"],
            address_line2=business_address["address_line2"],
            city=business_address["city"],
            state=business_address["state"],
            country=business_address["country"],
            postal_code=business_address["postal_code"],
        )

    return UserBusinessStatusResponse(
        account_type=auth_user.account_type,
        business_name=auth_user.business_name,
        tax_id=auth_user.tax_id,
        business_type=auth_user.business_type,
        business_verified=auth_user.business_verified,
        tax_exempt=auth_user.tax_exempt,
        tax_jurisdiction=auth_user.tax_jurisdiction,
        business_address=business_address_obj,
    )


@router.put("/user/account-type")
async def update_user_account_type(
    request: Request,
    body: UpdateAccountTypeRequest,
    session: Session = Depends(get_db_session),
):
    """Update user account type (individual vs business)."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    auth_user_dao = AuthUserDAO(session)

    try:
        if body.account_type == "business" and body.business_info:
            # Update to business account with business information
            auth_user_dao.update_account_type(
                user_id=user_id,
                account_type=body.account_type,
                business_name=body.business_info.business_name,
                tax_id=body.business_info.tax_id,
                business_type=body.business_info.business_type,
                business_address_line1=body.business_info.business_address.address_line1,
                business_address_line2=body.business_info.business_address.address_line2,
                business_city=body.business_info.business_address.city,
                business_state=body.business_info.business_address.state,
                business_country=body.business_info.business_address.country,
                business_postal_code=body.business_info.business_address.postal_code,
                tax_exempt=body.business_info.tax_exempt,
            )
        else:
            # Update to individual account (clears business info)
            auth_user_dao.update_account_type(
                user_id=user_id,
                account_type=body.account_type,
            )

        return {"message": f"Account type updated to {body.account_type} successfully"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/user/business-info")
async def update_user_business_info(
    request: Request,
    body: UpdateBusinessInfoRequest,
    session: Session = Depends(get_db_session),
):
    """Update business information for business accounts."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    auth_user_dao = AuthUserDAO(session)

    try:
        auth_user_dao.update_business_info(
            user_id=user_id,
            business_name=body.business_name,
            tax_id=body.tax_id,
            business_type=body.business_type,
            business_address_line1=(
                body.business_address.address_line1 if body.business_address else None
            ),
            business_address_line2=(
                body.business_address.address_line2 if body.business_address else None
            ),
            business_city=body.business_address.city if body.business_address else None,
            business_state=(
                body.business_address.state if body.business_address else None
            ),
            business_country=(
                body.business_address.country if body.business_address else None
            ),
            business_postal_code=(
                body.business_address.postal_code if body.business_address else None
            ),
            tax_exempt=body.tax_exempt,
        )

        return {"message": "Business information updated successfully"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@admin_router.post("/auth-user/verify-business")
async def verify_business_account(
    body: BusinessVerificationRequest,
    session: Session = Depends(get_db_session),
):
    """Admin endpoint to verify a business account."""
    auth_user_dao = AuthUserDAO(session)

    try:
        # TODO: Add actual business verification logic here
        # This could include:
        # - Tax ID validation via external services
        # - Business registration verification
        # - Address verification

        auth_user_dao.set_business_verified(
            user_id=body.user_id,
            verified=True,
            tax_jurisdiction="Determined by verification process",  # Replace with actual logic
        )

        return {"message": f"Business account {body.user_id} verified successfully"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@admin_router.get("/auth-user/business-accounts")
async def list_business_accounts(
    verified: Optional[bool] = Query(None, description="Filter by verification status"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    """Admin endpoint to list business accounts."""
    auth_user_dao = AuthUserDAO(session)

    if verified is not None:
        users = auth_user_dao.get_business_users_by_verification_status(
            verified=verified,
            limit=limit,
            offset=offset,
        )
    else:
        users = auth_user_dao.get_users_by_account_type(
            account_type="business",
            limit=limit,
            offset=offset,
        )

    return [
        {
            "id": user.id,
            "email": user.email,
            "business_name": user.business_name,
            "tax_id": user.tax_id,
            "business_verified": user.business_verified,
            "tax_exempt": user.tax_exempt,
            "created_at": user.created_at,
        }
        for user in users
    ]


@router.post("/user/create-with-business-info")
async def create_user_with_business_info(
    body: UpdateAccountTypeRequest,
    session: Session = Depends(get_db_session),
):
    """Create a new user with business classification (for signup flow)."""
    auth_user_dao = AuthUserDAO(session)

    # Check if user already exists
    existing_user = auth_user_dao.filter(email=body.email)
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="A user with this email already exists.",
        )

    try:
        if body.account_type == "business" and body.business_info:
            # Create business user
            auth_user_dao.create(
                email=body.email,
                name=body.name,
                last_name=body.last_name,
                account_type=body.account_type,
                business_name=body.business_info.business_name,
                tax_id=body.business_info.tax_id,
                business_type=body.business_info.business_type,
                business_address_line1=body.business_info.business_address.address_line1,
                business_address_line2=body.business_info.business_address.address_line2,
                business_city=body.business_info.business_address.city,
                business_state=body.business_info.business_address.state,
                business_country=body.business_info.business_address.country,
                business_postal_code=body.business_info.business_address.postal_code,
                tax_exempt=body.business_info.tax_exempt,
            )
        else:
            # Create individual user
            auth_user_dao.create(
                email=body.email,
                name=body.name,
                last_name=body.last_name,
                account_type=body.account_type or "individual",
            )

        session.commit()
        return {
            "message": f"User created successfully with account type: {body.account_type}",
        }

    except ValueError as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))


# -- Manage the approval status for user access to hiring assistants --
@router.post(
    "/user/assistant-hiring-approval",
    response_model=AssistantHiringApprovalResponse,
    status_code=200,
)
async def request_assistant_hiring_approval(
    request: Request,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    user_id = request.state.user_id
    user_row_proxy = auth_user_dao.get_by_id(user_id)  # This returns a RowProxy
    if not user_row_proxy:
        raise not_found("User")

    user_instance = user_row_proxy[0]  # Get the AuthUser ORM instance from the RowProxy

    current_status = user_instance.assistant_hiring_approval
    if current_status == "approved":
        return AssistantHiringApprovalResponse(
            message="Assistant hiring is already approved.",
            assistant_hiring_approval=current_status,
        )
    if current_status == "pending":
        return AssistantHiringApprovalResponse(
            message="Request for assistant hiring is already pending.",
            assistant_hiring_approval=current_status,
        )

    if auth_user_dao.set_assistant_hiring_approval(user_instance.id, "pending"):
        session.commit()
        return AssistantHiringApprovalResponse(
            message="Request for assistant hiring submitted. You've been added to the waitlist.",
            assistant_hiring_approval="pending",
        )
    else:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to update your hiring approval request status.",
        )


@admin_router.put(
    "/auth-user/{target_user_id}/assistant-hiring-approval/{status}",
    response_model=AssistantHiringApprovalResponse,
)
async def set_user_assistant_hiring_status(
    target_user_id: str,
    status: str,  # e.g., "approved", "pending", "rejected", "revoked"
    session: Session = Depends(get_db_session),
):
    if status not in ASSISTANT_HIRING_APPROVAL_STATUSES or status is None:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid status. Must be one of {', '.join(s for s in ASSISTANT_HIRING_APPROVAL_STATUSES if s is not None)}.",
        )

    auth_user_dao = AuthUserDAO(session)
    user = auth_user_dao.get_by_id(target_user_id)
    if not user:
        raise not_found(f"User ID: {target_user_id}")

    user_instance = user[0]
    if auth_user_dao.set_assistant_hiring_approval(target_user_id, status):
        session.commit()
        if status == "approved":
            try:
                email_recipient = user_instance.name or "there"
                to_email = user_instance.email
                email_subject = "Hire your first assistant"
                email_body = f"""
                <html>
                <body>
                    <p>Hey {email_recipient}, Dan from Unify here,</p>
                    <p>Just wanted to let you know that your request has been approved.</p>
                    <p>You can now <a href="https://console.unify.ai/team">hire your first AI assistant</a>! 🤖</p>
                    <p>Let me know if there's anything I can help with as you get started :)</p>
                    <p>My inbox is always open,<br>
                    Dan</p>
                </body>
                </html>
                """
                email_coroutine = send_email_async(to_email, email_subject, email_body)
                email_sending_task = asyncio.create_task(email_coroutine)

                def _log_email_task_exception(task: asyncio.Task) -> None:
                    try:
                        task.result()
                        logger.info(
                            f"Email sending task for user {target_user_id} completed (status: {task.done()}).",
                        )
                    except Exception as e:
                        logger.error(
                            f"Background email sending task for user {target_user_id} encountered an error: {e}",
                            exc_info=True,
                        )

                email_sending_task.add_done_callback(_log_email_task_exception)
                logger.info(f"Scheduled approval email for user {target_user_id}.")
            except Exception as e:
                logger.error(
                    f"Failed to schedule email for user {target_user_id} approval: {e}",
                )
        return AssistantHiringApprovalResponse(
            message=f"User {target_user_id} assistant hiring approval status set to '{status}'.",
            assistant_hiring_approval=status,
        )
    else:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to set hiring approval status for user {target_user_id}.",
        )


@admin_router.get(
    "/auth-user/assistant-hiring-approval",
    response_model=List[AssistantHiringApprovalUserStatus],
)
async def list_users_by_assistant_hiring_approval(
    status_filter: Optional[str] = Query(
        None,
        description=f"Filter by status: {', '.join(s for s in ASSISTANT_HIRING_APPROVAL_STATUSES if s is not None)}, 'none', or 'all'",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)

    users_to_return: List[AuthUser] = []  # This will hold AuthUser ORM instances

    if not status_filter or status_filter.lower() == "all":
        user_rows = auth_user_dao.filter(
            limit=limit,
            offset=offset,
        )  # No approval filter (uses sentinel default)
        users_to_return = [row[0] for row in user_rows if row]
    elif status_filter.lower() == "none":
        user_rows = auth_user_dao.filter(
            assistant_hiring_approval=None,
            limit=limit,
            offset=offset,
        )  # Explicitly filter for None
        users_to_return = [row[0] for row in user_rows if row]
    else:
        # For specific statuses like "pending", "approved", etc.
        valid_statuses_for_direct_filter = [
            s for s in ASSISTANT_HIRING_APPROVAL_STATUSES if s is not None
        ]
        if status_filter not in valid_statuses_for_direct_filter:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter. Must be one of {', '.join(valid_statuses_for_direct_filter)}, 'none', or 'all'.",
            )
        # get_users_by_assistant_hiring_approval already returns List[AuthUser] (ORM instances)
        users_to_return = auth_user_dao.get_users_by_assistant_hiring_approval(
            status_filter,
            limit=limit,
            offset=offset,
        )

    return [
        AssistantHiringApprovalUserStatus(
            id=user.id,
            email=user.email,
            name=user.name,
            assistant_hiring_approval=user.assistant_hiring_approval,
            created_at=user.created_at,
        )
        for user in users_to_return
    ]


# -- Manage one time approval links that grant automatic approval to users for hiring assistants --
@router.post(
    "/user/claim-assistant-hiring-one-time-link",
    response_model=AssistantHiringApprovalResponse,
    status_code=200,
)
async def claim_assistant_hiring_one_time_link(
    request: Request,
    payload: AssistantHiringOneTimeLinkClaimTokenRequest,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    user_id = request.state.user_id
    user_row_proxy = auth_user_dao.get_by_id(user_id)
    if not user_row_proxy:
        raise not_found("User")

    user_instance = user_row_proxy[0]
    users_dao = UsersDAO(session)
    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)

    link = token_dao.get_by_token(payload.token)
    if not link:
        raise not_found("Approval link token")

    # Check if user has ever claimed a link and received benefits
    if user_instance.has_claimed_approval_link:
        original_approval_status = user_instance.assistant_hiring_approval
        updated_approval_status = original_approval_status
        message = "You have already benefited from a one-time approval link. This link was not consumed, and no new credits were awarded."  # Default message

        was_re_activated = False
        if original_approval_status in ["revoked", "rejected", None, "pending"]:
            if not auth_user_dao.set_assistant_hiring_approval(
                user_instance.id,
                "approved",
            ):
                session.rollback()
                raise HTTPException(
                    status_code=500,
                    detail="Failed to re-activate approval status.",
                )
            session.commit()
            session.refresh(
                user_instance,
            )  # Refresh to get the latest state for user_instance
            updated_approval_status = "approved"
            was_re_activated = True  # Mark that re-activation happened

        if was_re_activated and original_approval_status in [
            "revoked",
            "rejected",
            None,
            "pending",
        ]:  # Check original status for message
            message = "Your assistant hiring access has been re-activated as you previously benefited from an approval link. This link was not consumed, and no new credits were awarded."

        return AssistantHiringApprovalResponse(
            message=message,
            assistant_hiring_approval=updated_approval_status,
        )

    # Link consumption logic (if user.has_claimed_approval_link is False)
    if link.user_id is not None:
        if link.user_id != user_instance.id:
            raise HTTPException(
                status_code=400,
                detail="Approval link has already been claimed by another user.",
            )
        return AssistantHiringApprovalResponse(
            message="You already used this specific approval link, but had not been marked as benefited. Status corrected.",
            assistant_hiring_approval=user_instance.assistant_hiring_approval,
        )

    if link.expires_at < datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(status_code=400, detail="Approval link has expired.")

    try:
        claimed_link = token_dao.claim_link(payload.token, user_instance.id)
        if not claimed_link:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail="Failed to claim approval link. It might be invalid, expired or already claimed by another.",
            )

        if not auth_user_dao.set_assistant_hiring_approval(
            user_instance.id,
            "approved",
        ):
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to set approval status.",
            )

        auth_user_dao.update(id=user_instance.id, has_claimed_approval_link=True)

        users_dao.recharge_credit(
            user_id=user_instance.id,
            quantity=float(settings.assistant_creation_cost),
        )

        session.commit()
        return AssistantHiringApprovalResponse(
            message="Approval link successfully claimed and credits awarded.",
            assistant_hiring_approval="approved",
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred during link processing: {str(e)}",
        )


@admin_router.post(
    "/assistant-hiring-one-time-link",
    response_model=AssistantHiringOneTimeLinkResponse,
    status_code=201,
)
async def create_assistant_hiring_one_time_link(
    payload: AssistantHiringApprovalCreateLinkRequest = Depends(),
    session: Session = Depends(get_db_session),
):
    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)
    if payload.expires_in_days <= 0:
        raise HTTPException(status_code=500, detail="Expiration days must be positive.")

    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=payload.expires_in_days,
    )
    link = token_dao.create(expires_at=expires_at)
    session.commit()
    session.refresh(link)
    return AssistantHiringOneTimeLinkResponse(
        id=link.id,
        token=link.token,
        expires_at=link.expires_at,
        claimed_at=link.claimed_at,
        user_id=link.user_id,
    )


@admin_router.get(
    "/assistant-hiring-one-time-link",
    response_model=List[AssistantHiringOneTimeLinkResponse],
)
async def list_assistant_hiring_one_time_link(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)
    links = token_dao.list_links(limit=limit, offset=offset)
    return [
        AssistantHiringOneTimeLinkResponse(
            id=link.id,
            token=link.token,
            expires_at=link.expires_at,
            claimed_at=link.claimed_at,
            user_id=link.user_id,
        )
        for link in links
    ]


@admin_router.delete("/assistant-hiring-one-time-link/{link_id}", status_code=204)
async def delete_assistant_hiring_one_time_link(
    link_id: str,
    session: Session = Depends(get_db_session),
):
    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)
    if not token_dao.delete_link(link_id):
        raise not_found("One-time approval link")
    session.commit()
    return None


@router.post("/user/validate-tax-id")
async def validate_tax_id(
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


@router.get("/user/supported-tax-countries")
async def get_supported_tax_countries():
    """Get list of countries supported for tax ID validation."""
    return {
        "supported_countries": TaxIDValidator.get_supported_countries(),
        "total_countries": len(TaxIDValidator.get_supported_countries()),
    }


@router.get("/user/onboarding-status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current user's onboarding status."""
    auth_user_dao = AuthUserDAO(session)
    user_row = auth_user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    auth_user = user_row[0]
    return OnboardingStatusResponse(onboarded=auth_user.onboarded)


@router.put("/user/onboarding-status")
async def update_onboarding_status(
    request: Request,
    body: UpdateOnboardingStatusRequest,
    session: Session = Depends(get_db_session),
):
    """Update the current user's onboarding status."""
    auth_user_dao = AuthUserDAO(session)
    user_row = auth_user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    auth_user_dao.update(id=request.state.user_id, onboarded=body.onboarded)
    session.commit()

    return {"message": "Onboarding status updated successfully"}
