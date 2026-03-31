import base64
import datetime
import logging
import secrets
from typing import List, Optional

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
from orchestra.db.dao.auth_dao import AuthDAO, decrypt_secret
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
from orchestra.web.api.assistant.schema import (
    SpendingLimitReachedRequest,
    SpendingLimitReachedResponse,
)
from orchestra.web.api.users.schema import (
    AccountDeletionConfirmation,
    AccountDeletionResponse,
    CanDeleteAccountResponse,
    CreditGrantClaimResponse,
    CreditGrantLinkClaimDetail,
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
    UserRequest,
    UserSpendingLimitRequest,
    UserSpendingLimitResponse,
    UserSpendResponse,
)
from orchestra.web.api.utils.http_responses import not_found

admin_router = APIRouter()
router = APIRouter()
logger = logging.getLogger(__name__)

# TODO: Move exceptions to exceptions file
# TODO: Fetch organization if it exists when reading user info
# TODO: Return tier in user info endpoints + double check rest of the information

# Endpoints used by next-auth


@admin_router.post("/user")
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

    # Initialize onboarding status for the new user
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_dao.create(user_id=new_user.id, current_step="workspace_setup")

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

    api_key = api_key_dao.filter(user_id=user_instance.id)
    if api_key:
        api_key_value = api_key[0][0].key
    else:
        # User exists but has no API key — create one to self-heal
        logger.warning(
            "User %s has no API key; generating one.",
            user_instance.id,
        )
        new_key = generate_key()
        api_key_dao.create(key=new_key, name="", user_id=user_instance.id)
        session.commit()
        api_key_value = new_key

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

    # Derive onboarding step from OnboardingStatus table
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_status = onboarding_dao.get_by_user_id(user_instance.id)
    onboarding_step = (
        onboarding_status.current_step if onboarding_status else "completed"
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
        "api_key": api_key_value,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        "onboarding_step": onboarding_step,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.get("/user/by-email")
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

    api_key = api_key_dao.filter(user_id=user_instance.id)
    if api_key:
        api_key_value = api_key[0][0].key
    else:
        # User exists but has no API key — create one to self-heal
        logger.warning(
            "User %s (%s) has no API key; generating one.",
            user_instance.id,
            email,
        )
        new_key = generate_key()
        api_key_dao.create(key=new_key, name="", user_id=user_instance.id)
        session.commit()
        api_key_value = new_key

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

    # Derive onboarding step from OnboardingStatus table
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_status = onboarding_dao.get_by_user_id(user_instance.id)
    onboarding_step = (
        onboarding_status.current_step if onboarding_status else "completed"
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
        "api_key": api_key_value,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        "onboarding_step": onboarding_step,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.get("/user/by-account")
def get_user_by_account(
    provider_account_id: str,
    provider: str,
    session: Session = Depends(get_db_session),
):
    auth_dao = AuthDAO(session)
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    organization_dao = OrganizationDAO(session)
    role_dao = RoleDAO(session)

    account = auth_dao.filter_oauth_accounts(
        provider_account_id=provider_account_id,
        provider=provider,
    )
    if not account:
        return None
    user = user_dao.filter(id=account[0][0].user_id)
    if not user:
        return None
    user_instance = user[0][0]

    api_key = api_key_dao.filter(user_id=user_instance.id)
    if api_key:
        api_key_value = api_key[0][0].key
    else:
        # User exists but has no API key — create one to self-heal
        logger.warning(
            "User %s has no API key; generating one.",
            user_instance.id,
        )
        new_key = generate_key()
        api_key_dao.create(key=new_key, name="", user_id=user_instance.id)
        session.commit()
        api_key_value = new_key

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

    # Derive onboarding step from OnboardingStatus table
    onboarding_dao = OnboardingStatusDAO(session)
    onboarding_status = onboarding_dao.get_by_user_id(user_instance.id)
    onboarding_step = (
        onboarding_status.current_step if onboarding_status else "completed"
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
        "api_key": api_key_value,
        "organizations": organizations,
        "has_claimed_credit_grant_link": has_claimed,
        "onboarding_step": onboarding_step,
        "timezone": user_instance.timezone,
        "phone_number": user_instance.phone_number,
    }


@admin_router.put("/user")
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
def delete_user(
    user_id: str,
    request: Request,
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

    If the user has MFA enabled, an ``x-mfa-code`` header with a valid
    TOTP code is required.  When the header is missing or invalid the
    endpoint returns ``403 { error: "mfa_required" }``.
    """
    # --- MFA gate for sensitive action ---
    auth_dao = AuthDAO(session)
    credential = auth_dao.get_enabled_totp(user_id)
    if credential:
        mfa_code = request.headers.get("x-mfa-code")
        mfa_recovery = request.headers.get("x-mfa-recovery-code")

        if not mfa_code and not mfa_recovery:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "mfa_required",
                    "message": "This action requires MFA verification.",
                },
            )

        verified = False

        if mfa_code:
            # Verify TOTP directly (without updating last_used_at)
            # to avoid a StaleDataError when the CASCADE delete removes
            # the credential row during account deletion.
            import pyotp

            secret = decrypt_secret(credential.credential_data)
            totp = pyotp.TOTP(secret)
            verified = totp.verify(mfa_code, valid_window=1)
        elif mfa_recovery:
            remaining = auth_dao.verify_recovery_code(user_id, mfa_recovery)
            verified = remaining is not None

        if not verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "mfa_required",
                    "message": "Invalid MFA code. Please try again.",
                },
            )
        # Remove the credential from the session so it doesn't interfere
        # with the CASCADE delete triggered by delete_user_account.
        session.expunge(credential)

    cleanup_service = UserAccountCleanupService(session)
    result = cleanup_service.delete_user_account(user_id, force_org_check=force)

    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)

    return AccountDeletionResponse(success=True, message=result.message)


### Not related to next-auth
# Note: link_account and unlink_account endpoints moved to auth/views.py


def generate_key(size=32):
    buffer = secrets.token_bytes(size)
    key = base64.b64encode(buffer).decode("utf-8")
    # Replace forward slashes with hyphens to avoid issues with URL encoding
    return key.replace("/", "-")


## Tier-setting endpoint has been moved to orchestra/web/api/admin/views.py
## under generalized PUT /billing/tier.  Backward-compat alias PUT /user/tier
## is registered there.


@admin_router.put("/user/quotas/reset")
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
    custom_key: Optional[str] = None,
    session: Session = Depends(get_db_session),
):
    api_key_dao = ApiKeyDAO(session)
    existing_api_key = api_key_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )
    if existing_api_key:
        raise HTTPException(
            status_code=400,
            detail="This user/organization already has an API key.",
        )
    if custom_key:
        existing_custom = api_key_dao.filter(key=custom_key)
        if existing_custom:
            raise HTTPException(
                status_code=400,
                detail="This API key value is already in use.",
            )
    new_api_key = custom_key or generate_key()
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
    custom_key: Optional[str] = None,
    session: Session = Depends(get_db_session),
):
    api_key_dao = ApiKeyDAO(session)
    old_api_key = api_key_dao.filter(user_id=user_id, organization_id=organization_id)
    api_key_dao.delete(id=old_api_key[0][0].id)
    if custom_key:
        existing_custom = api_key_dao.filter(key=custom_key)
        if existing_custom:
            raise HTTPException(
                status_code=400,
                detail="This API key value is already in use.",
            )
    new_api_key = custom_key or generate_key()
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


@router.post(
    "/user/photo/upload",
    status_code=status.HTTP_201_CREATED,
    summary="Upload user profile photo",
    tags=["Users"],
)
async def upload_user_photo(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
):
    from orchestra.services.bucket_service import BucketService

    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
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
    gcs_url = bucket_service.upload_user_photo_file(
        file_content=file_content,
        user_id=user_id,
        content_type=file.content_type,
    )

    user_dao = UserDAO(session)
    user_dao.update(id=user_id, image=gcs_url)

    return {"gcs_url": gcs_url}


@router.delete(
    "/user/photo",
    summary="Remove user profile photo",
    tags=["Users"],
)
def remove_user_photo(
    request: Request,
    session: Session = Depends(get_db_session),
):
    from orchestra.services.bucket_service import BucketService

    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    # Delete all photos for this user from the account photo bucket
    try:
        bucket_service = BucketService()
        bucket_service.delete_user_account_photos(user_id)
    except Exception as e:
        logger.error(f"Failed to delete GCS photos for user {user_id}: {e}")

    user_dao = UserDAO(session)
    user_dao.update(id=user_id, image=None)

    return {"message": "Profile photo removed."}


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
def claim_credit_grant_link(
    request: Request,
    payload: CreditGrantLinkClaimRequest,
    session: Session = Depends(get_db_session),
):
    """
    Claim a credit grant link.

    Credits are applied to the billing account that corresponds to the
    caller's active workspace:
    - Personal API key → user's BillingAccount
    - Organization API key → organization's BillingAccount

    Guards:
    - Per-link budget: number of claims must stay below max_claims.
    - Per-user lifetime: a user can only benefit from one link ever.
    - Per-org lifetime: an organization can only benefit from one link ever.
    """
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO
    from orchestra.db.dao.organization_dao import OrganizationDAO

    user_dao = UserDAO(session)
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

    user_row_proxy = user_dao.get_by_id(user_id)
    if not user_row_proxy:
        raise not_found("User")

    user_instance = user_row_proxy[0]
    token_dao = OneTimeCreditGrantLinkDAO(session)

    link = token_dao.get_by_token(payload.token)
    if not link:
        raise not_found("Credit grant link token")

    # --- Per-user lifetime guard ---
    if token_dao.has_user_claimed_any_link(user_id):
        return CreditGrantClaimResponse(
            message="You have already benefited from a credit grant link. "
            "This link was not consumed, and no new credits were awarded.",
            credits_granted=None,
        )

    # --- Per-org lifetime guard ---
    org_instance = None
    if organization_id:
        org_dao = OrganizationDAO(session)
        org_instance = org_dao.get(organization_id)
        if not org_instance:
            raise not_found("Organization")

        if token_dao.has_org_claimed_any_link(organization_id):
            return CreditGrantClaimResponse(
                message=f"The organization '{org_instance.name}' has already benefited "
                "from a credit grant link. "
                "This link was not consumed, and no new credits were awarded.",
                credits_granted=None,
            )

    # --- Per-link budget guard ---
    if token_dao.is_fully_redeemed(link):
        raise HTTPException(
            status_code=400,
            detail="This link has reached its redemption limit.",
        )

    # --- Same user already claimed this specific link ---
    if token_dao.has_user_claimed_link(link.id, user_instance.id):
        return CreditGrantClaimResponse(
            message="You already claimed this specific link.",
            credits_granted=None,
        )

    # --- Expiry check ---
    if link.expires_at < datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(status_code=400, detail="This link has expired.")

    try:
        claim = token_dao.claim_link(
            payload.token,
            user_instance.id,
            organization_id=organization_id,
        )
        if not claim:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail="Failed to claim link. It may be invalid, expired, or "
                "has reached its redemption limit.",
            )

        credit_amount = float(link.credit_amount)
        credited_to = "personal"
        ba_dao = BillingAccountDAO(session)

        if org_instance:
            ba = org_instance.billing_account
            if ba is None:
                ba = ba_dao.create()
                org_instance.billing_account_id = ba.id
                session.flush()
            ba_dao.apply_credit_grant(ba.id, credit_amount)
            credited_to = org_instance.name
        else:
            ba = user_instance.billing_account
            if ba is None:
                ba = ba_dao.create()
                user_instance.billing_account_id = ba.id
                session.flush()
            ba_dao.apply_credit_grant(ba.id, credit_amount)

        session.commit()
        return CreditGrantClaimResponse(
            message=f"Link successfully claimed! {credit_amount:.2f} credits awarded.",
            credits_granted=credit_amount,
            credited_to=credited_to,
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
    Create a credit grant link.

    When a user claims this link, they receive the specified credit_amount.
    If credit_amount is not provided, defaults to assistant_creation_cost.
    Set max_claims > 1 to allow the link to be shared with multiple users.
    """
    token_dao = OneTimeCreditGrantLinkDAO(session)
    if payload.expires_in_days <= 0:
        raise HTTPException(status_code=400, detail="Expiration days must be positive.")
    if payload.max_claims is not None and payload.max_claims < 1:
        raise HTTPException(status_code=400, detail="max_claims must be at least 1.")

    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=payload.expires_in_days,
    )
    link = token_dao.create(
        expires_at=expires_at,
        credit_amount=payload.credit_amount,
        max_claims=payload.max_claims,
        name=payload.name,
    )
    session.commit()
    session.refresh(link)
    return CreditGrantLinkResponse(
        id=link.id,
        token=link.token,
        name=link.name,
        expires_at=link.expires_at,
        credit_amount=link.credit_amount,
        max_claims=link.max_claims,
        claim_count=len(link.claims),
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
    """List all credit grant links with their claim details."""
    from orchestra.db.dao.organization_dao import OrganizationDAO

    token_dao = OneTimeCreditGrantLinkDAO(session)
    links = token_dao.list_links(limit=limit, offset=offset)

    # Collect all user_ids and org_ids from claims across all links
    all_user_ids: set = set()
    all_org_ids: set = set()
    for link in links:
        for claim in link.claims:
            all_user_ids.add(claim.user_id)
            if claim.organization_id:
                all_org_ids.add(claim.organization_id)

    # Batch-resolve emails
    email_map: dict = {}
    if all_user_ids:
        user_dao = UserDAO(session)
        for uid in all_user_ids:
            row = user_dao.get_by_id(uid)
            if row:
                email_map[uid] = row[0].email

    # Batch-resolve org names
    org_name_map: dict = {}
    if all_org_ids:
        org_dao = OrganizationDAO(session)
        for oid in all_org_ids:
            org = org_dao.get(oid)
            if org:
                org_name_map[oid] = org.name

    result = []
    for link in links:
        claim_details = [
            CreditGrantLinkClaimDetail(
                user_id=claim.user_id,
                organization_id=claim.organization_id,
                claimed_at=claim.claimed_at,
                claimed_by_email=email_map.get(claim.user_id),
                claimed_for_org=(
                    org_name_map.get(claim.organization_id)
                    if claim.organization_id
                    else None
                ),
            )
            for claim in link.claims
        ]
        result.append(
            CreditGrantLinkResponse(
                id=link.id,
                token=link.token,
                name=link.name,
                expires_at=link.expires_at,
                credit_amount=link.credit_amount,
                max_claims=link.max_claims,
                claim_count=len(link.claims),
                claims=claim_details,
            ),
        )
    return result


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


@router.get("/user/onboarding-status", response_model=OnboardingStatusResponse)
def get_onboarding_status(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Get the current user's onboarding status (derived from OnboardingStatus table)."""
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    onboarding_dao = OnboardingStatusDAO(session)
    status = onboarding_dao.get_by_user_id(request.state.user_id)
    onboarded = status.current_step == "completed" if status else True

    return OnboardingStatusResponse(onboarded=onboarded)


@router.put("/user/onboarding-status")
def update_onboarding_status(
    request: Request,
    body: UpdateOnboardingStatusRequest,
    session: Session = Depends(get_db_session),
):
    """Update the current user's onboarding status (syncs to OnboardingStatus table)."""
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(request.state.user_id)
    if not user_row:
        raise not_found("User")

    onboarding_dao = OnboardingStatusDAO(session)
    if body.onboarded:
        onboarding_dao.mark_completed(request.state.user_id)
    else:
        onboarding_dao.reset(request.state.user_id)

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

    When completing onboarding, grants signup promo credits to the
    billing account that matches the user's workspace choice:
    - personal  → user's billing account
    - organization → org's billing account
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

    # Grant signup promo credits when onboarding completes.
    # The console's axios interceptor converts camelCase → snake_case
    # before the request reaches here, so keys are always snake_case.
    if body.current_step == "completed" and body.step_data:
        selected_type = body.step_data.get("selected_type")
        if selected_type:
            org_id_str = body.step_data.get("organization_id")
            org_id: Optional[int] = None
            if org_id_str:
                try:
                    org_id = int(org_id_str)
                except (ValueError, TypeError):
                    org_id = None
            ba_dao = BillingAccountDAO(session)
            ba_dao.grant_signup_credits(
                user_id=request.state.user_id,
                selected_type=selected_type,
                organization_id=org_id,
            )

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


@router.get("/user/spend", response_model=UserSpendResponse)
async def get_user_spend(
    request: Request,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
) -> UserSpendResponse:
    """Get the current user's cumulative spend for a given month (personal context)."""
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise not_found("User")

    user = user_row[0]

    cumulative_spend = user_dao.get_cumulative_spend(user_id, month)
    limit = user_dao.get_spending_cap(user_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    credit_balance = None
    if user.billing_account:
        credit_balance = float(user.billing_account.credits)

    return UserSpendResponse(
        user_id=user_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=user.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
    )


@router.post(
    "/user/spending-limit-reached",
    response_model=SpendingLimitReachedResponse,
)
async def spending_limit_reached(
    request: Request,
    body: SpendingLimitReachedRequest,
    session: Session = Depends(get_db_session),
) -> SpendingLimitReachedResponse:
    """
    Notify users when a spending limit is reached.

    Called by Unity when a spending limit blocks an LLM call. Verifies the
    caller has access to the entity, then sends email notifications and
    records the notification for deduplication.
    """
    from orchestra.db.dao.assistant_dao import AssistantDAO
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.services.spending_limit_notification_service import (
        SpendingLimitNotificationService,
    )

    user_id = request.state.user_id

    if body.limit_type == "assistant":
        assistant_dao = AssistantDAO(session)
        assistant = assistant_dao.get_assistant_by_agent_id(int(body.entity_id))
        if not assistant or assistant.user_id != user_id:
            raise HTTPException(status_code=404, detail="Assistant not found.")
    elif body.limit_type == "user":
        if body.entity_id != user_id:
            raise HTTPException(
                status_code=403,
                detail="Cannot send notifications for another user.",
            )
    elif body.limit_type == "member":
        org_member_dao = OrganizationMemberDAO(session)
        member = org_member_dao.get_member(user_id, body.organization_id)
        if not member:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of this organization.",
            )
    elif body.limit_type == "organization":
        org_dao = OrganizationDAO(session)
        org = org_dao.get(int(body.entity_id))
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found.")
        org_member_dao = OrganizationMemberDAO(session)
        member = org_member_dao.get_member(user_id, int(body.entity_id))
        is_owner = org.owner_id == user_id
        if not member and not is_owner:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of this organization.",
            )

    notification_service = SpendingLimitNotificationService(session)

    result = notification_service.process_limit_reached(
        limit_type=body.limit_type,
        entity_id=body.entity_id,
        limit_value=body.limit_value,
        current_spend=body.current_spend,
        month=body.month,
        limit_set_at=body.limit_set_at,
        entity_name=body.entity_name,
        organization_id=body.organization_id,
    )

    if result.notified:
        session.commit()

    return SpendingLimitReachedResponse(
        notified=result.notified,
        reason=result.reason,
        recipient_count=result.recipient_count,
        notified_user_ids=result.notified_user_ids,
    )


# ============================================================================
# Backward-Compat Stub Endpoints
# ============================================================================
# These stubs preserve the old API surface so that external repos (console,
# ivory, etc.) continue to work after the underlying models and logic have
# been refactored.  They should be removed once all callers have migrated.
# ============================================================================


@admin_router.post("/user/verify-business")
def _compat_verify_business_account(
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (old verify-business flow removed)."""
    return {"message": "Business account verification is no longer required (no-op)."}


@admin_router.get("/user/business-accounts")
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
