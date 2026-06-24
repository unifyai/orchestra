import base64
import datetime
import logging
import secrets
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.exc import IntegrityError
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
from orchestra.db.dao.referral_dao import ReferralDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.lib.referrals import ReferralError, attribute_referral
from orchestra.services.coordinator_service import (
    ensure_coordinator_intro_watched,
    ensure_personal_coordinator_provisioned,
    ensure_workspace_coordinator_provisioned,
    get_workspace_coordinator,
    list_coordinators_missing_intro_watched,
    list_workspace_memberships_missing_coordinator,
)
from orchestra.services.user_account_cleanup_service import (
    UserAccountCleanupService,
    run_user_runtime_cleanup_tasks,
)
from orchestra.settings import settings
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
    PhoneVerificationConfirm,
    PhoneVerificationRequest,
    QueryLoggingStatus,
    ReferralAttributionRequest,
    ReferralAttributionResponse,
    ReferralCodeResponse,
    ReferralCreateCodeRequest,
    ReferralListItem,
    ReferralListResponse,
    ReferralSummaryResponse,
    UpdateOnboardingStatusRequest,
    UpdateQueryLoggingRequest,
    UserRequest,
    UserSpendingLimitRequest,
    UserSpendingLimitResponse,
    UserSpendResponse,
)
from orchestra.web.api.utils.assistant_infra import delete_pubsub_topic
from orchestra.web.api.utils.http_responses import not_found

admin_router = APIRouter()
router = APIRouter()
logger = logging.getLogger(__name__)

# TODO: Move exceptions to exceptions file
# TODO: Fetch organization if it exists when reading user info

# Endpoints used by next-auth


@admin_router.post("/user")
async def create_user(
    user: UserRequest,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    api_key_dao = ApiKeyDAO(session)
    created_coordinator = False
    coordinator_id: int | None = None

    try:
        user_dao.create(
            email=user.email,
            name=user.name,
            last_name=user.last_name,
            job_title=user.job_title,
            bio=user.bio,
            image=user.image,
            timezone=user.timezone,
            phone_number=user.phone_number,
            whatsapp_number=user.whatsapp_number,
            discord_id=user.discord_id,
        )
        user_row = user_dao.filter(email=user.email)
        new_user = user_row[0][0]

        new_api_key = generate_key()
        api_key_dao.create(key=new_api_key, name="", user_id=new_user.id)

        # Seed default Droid project, interface, tab, and table tile for tasks
        try:
            DefaultTasksSeeder.seed(session, user_id=new_user.id)
        except Exception as e:
            print(e)

        # Initialize onboarding status for the new user
        onboarding_dao = OnboardingStatusDAO(session)
        onboarding_dao.create(user_id=new_user.id, current_step="workspace_setup")

        coordinator, created_coordinator = (
            await ensure_personal_coordinator_provisioned(
                session,
                user_id=str(new_user.id),
            )
        )
        coordinator_id = coordinator.agent_id
        session.commit()
    except Exception:
        session.rollback()
        if created_coordinator and coordinator_id is not None:
            await delete_pubsub_topic(str(coordinator_id))
        raise

    if created_coordinator:
        # Best-effort welcome email from the new user's Coordinator.
        # Runs post-commit so a mail hiccup can never undo the signup.
        try:
            from orchestra.routines.inactivity_notifications import (
                send_coordinator_welcome_email,
            )

            await send_coordinator_welcome_email(
                recipient_email=new_user.email,
                owner_first_name=new_user.name,
            )
        except Exception:
            logger.warning(
                "Failed to send Coordinator welcome email for user %s",
                new_user.id,
                exc_info=True,
            )

    return {
        "id": new_user.id,
        "name": new_user.name,
        "bio": new_user.bio,
        "image": new_user.image,
        "email": new_user.email,
        "timezone": new_user.timezone,
        "phone_number": new_user.phone_number,
        "whatsapp_number": new_user.whatsapp_number,
        "discord_id": new_user.discord_id,
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

    personal_keys = api_key_dao.get_personal_keys(user_instance.id)
    if personal_keys:
        api_key_value = personal_keys[0][0].key
    else:
        logger.warning(
            "User %s has no personal API key; generating one.",
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
        "whatsapp_number": user_instance.whatsapp_number,
        "discord_id": user_instance.discord_id,
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

    personal_keys = api_key_dao.get_personal_keys(user_instance.id)
    if personal_keys:
        api_key_value = personal_keys[0][0].key
    else:
        logger.warning(
            "User %s (%s) has no personal API key; generating one.",
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
        "whatsapp_number": user_instance.whatsapp_number,
        "discord_id": user_instance.discord_id,
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

    personal_keys = api_key_dao.get_personal_keys(user_instance.id)
    if personal_keys:
        api_key_value = personal_keys[0][0].key
    else:
        logger.warning(
            "User %s has no personal API key; generating one.",
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
        "whatsapp_number": user_instance.whatsapp_number,
        "discord_id": user_instance.discord_id,
    }


async def _reawaken_user_assistants(session: Session, user_id: str) -> None:
    """Refresh the live runtime of a user's personal assistants after a contact
    identity (phone / WhatsApp / Discord) change.

    The reawaken webhook re-fetches the user's current numbers and pushes an
    assistant update into the runtime, so the boss contact and session details
    pick up the new value without a coordinator restart. Best-effort: failures
    are logged and never block the profile update.
    """
    from orchestra.db.models.orchestra_models import Assistant
    from orchestra.web.api.utils.assistant_infra import reawaken_assistant

    agent_ids = [
        row[0]
        for row in session.query(Assistant.agent_id)
        .filter(
            Assistant.user_id == user_id,
            Assistant.organization_id.is_(None),
        )
        .all()
    ]
    for agent_id in agent_ids:
        try:
            await reawaken_assistant(str(agent_id))
        except Exception as exc:
            logging.warning(
                "Failed to reawaken assistant %s after user profile update: %s",
                agent_id,
                exc,
            )


@admin_router.put("/user")
async def update_user(
    updated_user: UserRequest,
    session: Session = Depends(get_db_session),
):
    user_dao = UserDAO(session)
    user_rows = user_dao.filter(id=updated_user.user_id)
    if not user_rows:
        raise not_found("User")
    user = user_rows[0][0]

    # Snapshot routable identities before the update so we can detect a real
    # change and refresh the running assistants' boss contact accordingly.
    old_phone_number = user.phone_number
    old_whatsapp_number = user.whatsapp_number
    old_discord_id = user.discord_id

    # Require server-side verification when phone or whatsapp changes
    try:
        user_dao.require_verified_phone(user, updated_user.phone_number, "phone")
        user_dao.require_verified_phone(user, updated_user.whatsapp_number, "whatsapp")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    from orchestra.web.api.utils.phone_number_validator import validate_phone_number

    def _normalize_phone(val: str | None) -> str | None:
        if not val:
            return val
        r = validate_phone_number(val)
        return r["formatted_phone_number"] if r["is_valid"] else val

    # Only pass fields that were explicitly provided in the request body.
    # Fields not sent by the client default to None in the Pydantic model,
    # but user_dao.update() uses ... (Ellipsis) as a sentinel for "not
    # provided". Passing None for unset fields would overwrite existing
    # values with NULL.
    provided = updated_user.model_fields_set
    update_kwargs: dict = {"id": updated_user.user_id}
    if "name" in provided:
        update_kwargs["name"] = updated_user.name
    if "last_name" in provided:
        update_kwargs["last_name"] = updated_user.last_name
    if "job_title" in provided:
        update_kwargs["job_title"] = updated_user.job_title
    if "bio" in provided:
        update_kwargs["bio"] = updated_user.bio
    if "timezone" in provided:
        update_kwargs["timezone"] = updated_user.timezone
    if "phone_number" in provided:
        update_kwargs["phone_number"] = _normalize_phone(updated_user.phone_number)
    if "whatsapp_number" in provided:
        update_kwargs["whatsapp_number"] = _normalize_phone(
            updated_user.whatsapp_number,
        )
    if "discord_id" in provided:
        update_kwargs["discord_id"] = updated_user.discord_id

    # A routable contact identity change must reach the running runtime so the
    # boss contact resolves inbound from the new number/handle without a restart.
    contact_identity_changed = (
        (
            "phone_number" in update_kwargs
            and update_kwargs["phone_number"] != old_phone_number
        )
        or (
            "whatsapp_number" in update_kwargs
            and update_kwargs["whatsapp_number"] != old_whatsapp_number
        )
        or (
            "discord_id" in update_kwargs
            and update_kwargs["discord_id"] != old_discord_id
        )
    )

    user_dao.update(**update_kwargs)

    user_dao.cleanup_phone_verifications(updated_user.user_id)

    if contact_identity_changed:
        await _reawaken_user_assistants(session, updated_user.user_id)

    return "User information updated successfully!"


# ---------------------------------------------------------------------------
# Phone / WhatsApp number verification
# ---------------------------------------------------------------------------

VERIFICATION_EXPIRY_MINUTES = 10
VERIFICATION_MAX_ATTEMPTS = 5
VERIFICATION_COOLDOWN_SECONDS = 60


@admin_router.post("/user/phone/send-verification")
async def send_phone_verification(
    body: PhoneVerificationRequest,
    session: Session = Depends(get_db_session),
):
    """
    Send a verification SMS to a phone number.

    Delegates to the communication service for SMS delivery and to
    ``UserDAO`` for cooldown checks and record persistence.
    """
    import hashlib
    import os

    import httpx

    from orchestra.web.api.utils.http_client import get_async_client

    user_dao = UserDAO(session)
    user_rows = user_dao.filter(id=body.user_id)
    if not user_rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    if user_dao.check_verification_cooldown(
        body.user_id,
        body.phone_number,
        body.phone_type,
        VERIFICATION_COOLDOWN_SECONDS,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Please wait before requesting another verification code.",
        )

    # Call communication service — it generates the code, sends SMS,
    # and returns the code so we can hash and store it server-side.
    comms_url = os.environ.get("DROID_COMMS_URL", "")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")

    if not comms_url or not admin_key:
        logger.error("DROID_COMMS_URL or ORCHESTRA_ADMIN_KEY not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Verification service is not configured.",
        )

    try:
        client = get_async_client()
        response = await client.post(
            f"{comms_url}/social/verify",
            headers={
                "Authorization": f"Bearer {admin_key}",
                "Content-Type": "application/json",
            },
            json={
                "platform": body.phone_type,
                "account_identifier": body.phone_number,
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            logger.error(
                "Communication service returned %s: %s",
                response.status_code,
                response.text,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to send verification SMS.",
            )
        comms_data = response.json()
        code = comms_data.get("verification_code") or comms_data.get(
            "verificationCode",
        )
        if not code:
            logger.error("Communication service did not return a verification code")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to send verification SMS.",
            )
    except httpx.HTTPError as exc:
        logger.error("Failed to reach communication service: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send verification SMS.",
        )

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    user_dao.create_phone_verification(
        body.user_id,
        body.phone_number,
        body.phone_type,
        code_hash,
        VERIFICATION_EXPIRY_MINUTES,
    )

    return {
        "detail": "Verification code sent.",
        "expires_in_seconds": VERIFICATION_EXPIRY_MINUTES * 60,
    }


@admin_router.post("/user/phone/confirm-verification")
def confirm_phone_verification(
    body: PhoneVerificationConfirm,
    session: Session = Depends(get_db_session),
):
    """
    Confirm a phone verification code.

    On success, marks the verification record as verified so that a
    subsequent ``PUT /user`` can accept the new phone number.
    """
    user_dao = UserDAO(session)
    success, message = user_dao.confirm_phone_verification(
        body.user_id,
        body.phone_number,
        body.phone_type,
        body.code,
        VERIFICATION_MAX_ATTEMPTS,
    )

    if not success:
        if "No pending" in message:
            code = status.HTTP_404_NOT_FOUND
        elif "Too many" in message:
            code = status.HTTP_429_TOO_MANY_REQUESTS
        else:
            code = status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    return {"detail": message}


@admin_router.delete("/user", response_model=AccountDeletionResponse)
def delete_user(
    user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
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

    if result.cleanup_task_ids:
        background_tasks.add_task(
            run_user_runtime_cleanup_tasks,
            request.app.state.db_session_factory,
            cleanup_task_ids=result.cleanup_task_ids,
            user_id=user_id,
        )

    return AccountDeletionResponse(
        success=True,
        message=result.message,
        runtime_cleanup_complete=result.runtime_cleanup_complete,
        runtime_cleanup_summary=result.runtime_cleanup_summary,
    )


### Not related to next-auth
# Note: link_account and unlink_account endpoints moved to auth/views.py


def generate_key(size=32):
    buffer = secrets.token_bytes(size)
    key = base64.b64encode(buffer).decode("utf-8")
    # Replace forward slashes with hyphens to avoid issues with URL encoding
    return key.replace("/", "-")


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
    from orchestra.web.api.utils.safe_text import validate_safe_text

    try:
        name = validate_safe_text(name, max_length=255, allow_empty=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid API key name: {exc}",
        )
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
    from orchestra.web.api.utils.safe_text import validate_safe_text

    try:
        name = validate_safe_text(name, max_length=255, allow_empty=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid API key name: {exc}",
        )
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
async def create_organization(
    name: str,
    owner_id: str,
    session: Session = Depends(get_db_session),
):
    # This legacy endpoint takes ``name`` as a query param, so it bypasses the
    # Pydantic body validation used elsewhere. Validate it explicitly to block
    # HTML/script injection in stored organization names (defense-in-depth).
    from orchestra.web.api.utils.safe_text import validate_safe_text

    try:
        name = validate_safe_text(name, max_length=255)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid organization name: {exc}",
        )

    organization_dao = OrganizationDAO(session)
    user_dao = UserDAO(session)

    existing_org = organization_dao.filter(owner_id=owner_id)
    if existing_org:
        raise HTTPException(
            status_code=400,
            detail="This user already has an organization.",
        )

    # Get owner's timezone to initialize org timezone and reuse canonical org creation.
    owner_row = user_dao.get_by_id(owner_id) if owner_id else None
    owner_timezone = owner_row[0].timezone if owner_row else None

    from orchestra.web.api.organization.views import (
        _create_organization_with_owner_coordinator,
    )

    await _create_organization_with_owner_coordinator(
        session,
        name=name,
        owner_user_id=owner_id,
        timezone=owner_timezone,
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
    from orchestra.services.bucket_service import create_bucket_service

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

    bucket_service = create_bucket_service()
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
    from orchestra.services.bucket_service import create_bucket_service

    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    # Delete all photos for this user from the account photo bucket
    try:
        bucket_service = create_bucket_service()
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
        "whatsapp_number": user.whatsapp_number,
        "discord_id": user.discord_id,
    }


@router.post("/user/{user_id}/coordinator", status_code=status.HTTP_201_CREATED)
async def create_personal_coordinator_endpoint(
    user_id: str,
    request: Request,
    response: Response,
    organization_id: int | None = Query(None),
    preferred_phone_country: str | None = Query(None),
    session: Session = Depends(get_db_session),
) -> dict:
    """Create or return the authenticated user's workspace Coordinator."""
    if request.state.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create a Coordinator for another user.",
        )

    user_dao = UserDAO(session)
    if not user_dao.get_by_id(user_id):
        raise not_found("User")

    if organization_id is not None:
        org_dao = OrganizationDAO(session)
        org_member_dao = OrganizationMemberDAO(session)
        org = org_dao.get(organization_id)
        if org is None:
            raise not_found("Organization")
        if (
            org.owner_id != user_id
            and org_member_dao.get_member(
                user_id,
                organization_id,
            )
            is None
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot create a Coordinator outside your workspaces.",
            )

    from orchestra.db.models.orchestra_models import SharedPoolNumber
    from orchestra.services.universal_droid_discord import (
        get_universal_droid_discord_bot_id,
        notify_comms_discord_sync,
    )

    universal_discord_bot_id = get_universal_droid_discord_bot_id()
    discord_pool_existed_before = bool(universal_discord_bot_id) and (
        session.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "discord",
            SharedPoolNumber.number == universal_discord_bot_id,
        )
        .first()
        is not None
    )

    existing = get_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    response.status_code = (
        status.HTTP_200_OK if existing is not None else status.HTTP_201_CREATED
    )

    created_coordinator = False
    coordinator_id: int | None = None
    try:
        coordinator, created_coordinator = (
            await ensure_workspace_coordinator_provisioned(
                session,
                user_id=user_id,
                organization_id=organization_id,
                preferred_phone_country=preferred_phone_country,
            )
        )
        coordinator_id = coordinator.agent_id
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        conflict_keys = (
            "ux_assistants_one_personal_coordinator_per_user",
            "ux_assistants_one_workspace_coordinator_per_membership",
        )
        if not any(key in str(exc.orig) for key in conflict_keys):
            if created_coordinator and coordinator_id is not None:
                await delete_pubsub_topic(str(coordinator_id))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Failed to create Coordinator.",
            )
        if created_coordinator and coordinator_id is not None:
            await delete_pubsub_topic(str(coordinator_id))
            created_coordinator = False
        response.status_code = status.HTTP_200_OK
        coordinator = get_workspace_coordinator(
            session,
            user_id=user_id,
            organization_id=organization_id,
            preferred_phone_country=preferred_phone_country,
        )
        if coordinator is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Failed to create Coordinator.",
            )
        try:
            coordinator, created_coordinator = (
                await ensure_workspace_coordinator_provisioned(
                    session,
                    user_id=user_id,
                    organization_id=organization_id,
                )
            )
            coordinator_id = coordinator.agent_id
            session.commit()
        except Exception:
            session.rollback()
            if created_coordinator and coordinator_id is not None:
                await delete_pubsub_topic(str(coordinator_id))
            raise
    except Exception:
        session.rollback()
        if created_coordinator and coordinator_id is not None:
            await delete_pubsub_topic(str(coordinator_id))
        raise

    # Ask Droid to (re)connect the shared Coordinator Discord bot when this
    # request either created a new Coordinator or seeded the universal pool
    # row for the first time. Droid reads committed pool state over the admin
    # API, so this must run after the commits above. Best-effort.
    if universal_discord_bot_id and (
        created_coordinator or not discord_pool_existed_before
    ):
        await notify_comms_discord_sync()

    return {"coordinator_id": str(coordinator.agent_id)}


@admin_router.post("/coordinator/workspace/backfill")
@admin_router.post("/coordinator/personal/backfill")
async def backfill_workspace_coordinators(
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(True),
    session: Session = Depends(get_db_session),
) -> dict:
    """Backfill missing workspace Coordinators for existing users and memberships."""
    target_memberships = list_workspace_memberships_missing_coordinator(
        session,
        limit=limit,
    )
    if dry_run:
        return {
            "dry_run": True,
            "target_count": len(target_memberships),
            "targets": [
                {
                    "user_id": user_id,
                    "organization_id": organization_id,
                }
                for user_id, organization_id in target_memberships
            ],
        }

    created = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for user_id, organization_id in target_memberships:
        created_coordinator = False
        coordinator_id: int | None = None
        try:
            coordinator, created_coordinator = (
                await ensure_workspace_coordinator_provisioned(
                    session,
                    user_id=user_id,
                    organization_id=organization_id,
                    initial_intro_watched=True,
                )
            )
            coordinator_id = coordinator.agent_id
            session.commit()
            if created_coordinator:
                created += 1
            else:
                skipped += 1
        except Exception as exc:
            session.rollback()
            if created_coordinator and coordinator_id is not None:
                await delete_pubsub_topic(str(coordinator_id))
            errors.append(
                {
                    "user_id": user_id,
                    "organization_id": (
                        str(organization_id) if organization_id is not None else "null"
                    ),
                    "error": str(exc),
                },
            )

    return {
        "dry_run": False,
        "target_count": len(target_memberships),
        "created": created,
        "skipped_existing": skipped,
        "failed": len(errors),
        "errors": errors,
    }


@admin_router.post("/coordinator/intro-watched/backfill")
async def backfill_coordinator_intro_watched(
    limit: int = Query(500, ge=1, le=5000),
    dry_run: bool = Query(True),
    session: Session = Depends(get_db_session),
) -> dict:
    """Mark existing Coordinators as having resolved the intro picker."""
    coordinators = list_coordinators_missing_intro_watched(session, limit=limit)
    if dry_run:
        return {
            "dry_run": True,
            "target_count": len(coordinators),
            "targets": [
                {
                    "coordinator_id": coordinator.agent_id,
                    "user_id": coordinator.user_id,
                    "organization_id": coordinator.organization_id,
                }
                for coordinator in coordinators
            ],
        }

    updated = 0
    errors: list[dict[str, str]] = []
    for coordinator in coordinators:
        try:
            if ensure_coordinator_intro_watched(session, coordinator=coordinator):
                updated += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(
                {
                    "coordinator_id": str(coordinator.agent_id),
                    "user_id": coordinator.user_id,
                    "organization_id": (
                        str(coordinator.organization_id)
                        if coordinator.organization_id is not None
                        else "null"
                    ),
                    "error": str(exc),
                },
            )

    return {
        "dry_run": False,
        "target_count": len(coordinators),
        "updated": updated,
        "failed": len(errors),
        "errors": errors,
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
    include_in_schema=False,
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
    from orchestra.db.models.orchestra_models import OneTimeCreditGrantLink
    from orchestra.db.models.orchestra_models import Organization as OrganizationModel
    from orchestra.db.models.orchestra_models import User as UserModel

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

    # Acquire row locks to serialize concurrent claims and prevent
    # TOCTOU races on the per-user lifetime, per-org lifetime, and
    # per-link max_claims guards.
    session.query(UserModel).filter(
        UserModel.id == user_id,
    ).with_for_update().first()
    session.query(OneTimeCreditGrantLink).filter(
        OneTimeCreditGrantLink.id == link.id,
    ).with_for_update().first()

    org_instance = None
    if organization_id:
        session.query(OrganizationModel).filter(
            OrganizationModel.id == organization_id,
        ).with_for_update().first()

    # --- Per-user lifetime guard ---
    if token_dao.has_user_claimed_any_link(user_id):
        return CreditGrantClaimResponse(
            message="You have already benefited from a credit grant link. "
            "This link was not consumed, and no new credits were awarded.",
            credits_granted=None,
        )

    # --- Per-org lifetime guard ---
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
                ba = ba_dao.create(apply_signup_grant=False)
                org_instance.billing_account_id = ba.id
                session.flush()
            ba_dao.apply_credit_grant(ba.id, credit_amount)
            credited_to = org_instance.name
        else:
            ba = user_instance.billing_account
            if ba is None:
                ba = ba_dao.create(apply_signup_grant=False)
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


@router.get("/user/referral", response_model=ReferralSummaryResponse)
def get_referral_summary(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Return the caller's referral link, codes, and reward stats.

    A primary code is created lazily on first call so the user always has a
    link to share.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    dao = ReferralDAO(session)
    primary = dao.get_or_create_primary_code(user_id, organization_id)
    session.commit()

    codes = dao.list_codes(user_id, organization_id)
    attributions = dao.list_for_referrer(user_id, organization_id)
    pending = sum(1 for a in attributions if a.status == "pending")
    rewarded = sum(1 for a in attributions if a.status == "rewarded")

    return ReferralSummaryResponse(
        code=primary.code,
        referral_url=f"{settings.console_url}/login?ref={primary.code}",
        codes=[
            ReferralCodeResponse(
                code=c.code,
                label=c.label,
                created_at=c.created_at,
                disabled=c.disabled_at is not None,
            )
            for c in codes
        ],
        pending_count=pending,
        rewarded_count=rewarded,
        total_credits_earned=dao.total_credits_earned(user_id, organization_id),
        reward_credits=settings.referral_reward_credits,
        referee_bonus_credits=settings.referral_referee_bonus_credits,
        qualifying_spend=settings.referral_qualifying_spend,
    )


@router.post(
    "/user/referral/codes",
    response_model=ReferralCodeResponse,
    status_code=201,
)
def create_referral_code(
    payload: ReferralCreateCodeRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Create an additional referral code (e.g. one per channel/campaign).

    Scoped to the active workspace: in an organization workspace the code is
    org-owned and its rewards are credited to the organization.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    dao = ReferralDAO(session)
    code = dao.create_code(
        user_id,
        label=payload.label,
        organization_id=organization_id,
    )
    session.commit()
    return ReferralCodeResponse(
        code=code.code,
        label=code.label,
        created_at=code.created_at,
        disabled=False,
    )


@router.post(
    "/user/referral/attribute",
    response_model=ReferralAttributionResponse,
)
def attribute_referral_code(
    payload: ReferralAttributionRequest,
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Apply a referral code to the (new) caller's account.

    Idempotent: safe to retry on every login until it succeeds, mirroring
    the credit-grant-link claim flow. Attribution only — the reward is
    granted later, when the caller makes their first qualifying payment.
    """
    user_id = request.state.user_id
    user_dao = UserDAO(session)
    user_row = user_dao.get_by_id(user_id)
    if not user_row:
        raise not_found("User")
    user_instance = user_row[0]

    signup_ip = request.client.host if request.client else None
    try:
        result = attribute_referral(
            session,
            referee_user=user_instance,
            code=payload.code,
            signup_ip=signup_ip,
        )
        session.commit()
    except ReferralError as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=e.message)
    except Exception:
        session.rollback()
        raise

    return ReferralAttributionResponse(
        attributed=result.attributed,
        message=result.message,
        code=result.code,
    )


@router.get("/user/referrals", response_model=ReferralListResponse)
def list_referrals(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """List the caller's referrals (as referrer) and their reward status.

    Scoped to the active workspace (personal vs the current organization).
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    dao = ReferralDAO(session)
    items = [
        ReferralListItem(
            status=a.status,
            created_at=a.created_at,
            rewarded_at=a.rewarded_at,
            reward_amount=(
                float(a.reward_amount) if a.reward_amount is not None else None
            ),
        )
        for a in dao.list_for_referrer(user_id, organization_id)
    ]
    return ReferralListResponse(referrals=items)


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


@router.get(
    "/user/onboarding-status",
    response_model=OnboardingStatusResponse,
    include_in_schema=False,
)
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


@router.put("/user/onboarding-status", include_in_schema=False)
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


@router.get(
    "/user/onboarding",
    response_model=OnboardingStatusDetailedResponse,
    include_in_schema=False,
)
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


@router.put(
    "/user/onboarding",
    response_model=OnboardingStatusDetailedResponse,
    include_in_schema=False,
)
def update_onboarding_progress(
    request: Request,
    body: OnboardingStatusUpdateRequest,
    session: Session = Depends(get_db_session),
):
    """
    Update the current user's onboarding progress.

    The step_data is validated based on the current_step to ensure
    only valid fields are stored.

    Billing accounts are funded when they are created, before any
    credit-consuming onboarding work can run.
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

    session.commit()

    return OnboardingStatusDetailedResponse(
        user_id=status.user_id,
        current_step=status.current_step,
        step_data=OnboardingStepDataResponse(**(status.step_data or {})),
        created_at=status.created_at,
        updated_at=status.updated_at,
    )


@router.delete("/user/onboarding", include_in_schema=False)
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


@router.get(
    "/user/can-delete-account",
    response_model=CanDeleteAccountResponse,
    include_in_schema=False,
)
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


@router.delete(
    "/user/delete-account",
    response_model=AccountDeletionResponse,
    include_in_schema=False,
)
def delete_own_account(
    request: Request,
    body: AccountDeletionConfirmation,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
):
    """
    Delete the current user's account (self-service).

    Requires email confirmation to prevent accidental deletion.
    Permanently removes all user data - this action cannot be undone.

    Assistant deletion follows creator-owned lifecycle semantics. If this user
    created org-scoped assistants, those assistant rows cascade with the user
    row and are cleaned up by the same account-deletion flow.

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

    if result.cleanup_task_ids:
        background_tasks.add_task(
            run_user_runtime_cleanup_tasks,
            request.app.state.db_session_factory,
            cleanup_task_ids=result.cleanup_task_ids,
            user_id=request.state.user_id,
        )

    return AccountDeletionResponse(
        success=True,
        message=result.message,
        runtime_cleanup_complete=result.runtime_cleanup_complete,
        runtime_cleanup_summary=result.runtime_cleanup_summary,
    )


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
    billing_mode = "CREDITS"
    if user.billing_account:
        credit_balance = float(user.billing_account.credits)
        billing_mode = (
            BillingAccountDAO(session).resolve_billing_mode(user.billing_account).value
        )

    return UserSpendResponse(
        user_id=user_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=user.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
        billing_mode=billing_mode,
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

    Called by Droid when a spending limit blocks an LLM call. Verifies the
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


@router.post("/user/create-with-business-info", include_in_schema=False)
def _compat_create_user_with_business_info(
    session: Session = Depends(get_db_session),
):
    """Backward-compat stub: no-op (use POST /user with standard flow instead)."""
    return {"message": "Use the standard user creation flow (POST /admin/user)."}
