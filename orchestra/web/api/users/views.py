import base64
import datetime
import secrets
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.auth_user_dao import (
    AuthUserDAO,
    ASSISTANT_HIRING_APPROVAL_STATUSES,
)
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.assistant_hiring_one_time_approval_link_dao import (
    AssistantHiringOneTimeApprovalLinkDAO,
)
from orchestra.db.dependencies import get_db_session
from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.web.api.users.schema import (
    AccountRequest,
    FreezeAccountRequest,
    QueryLoggingStatus,
    UpdateQueryLoggingRequest,
    UserRequest,
    AssistantHiringApprovalCreateLinkRequest,
    AssistantHiringApprovalResponse,
    AssistantHiringApprovalUserStatus,
    AssistantHiringOneTimeLinkResponse,
    AssistantHiringOneTimeLinkClaimTokenRequest,
)
from orchestra.web.api.utils.http_responses import not_found
from orchestra.web.api.assistant.views import ASSISTANT_CREATION_COST

admin_router = APIRouter()
router = APIRouter()
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

    auth_user_dao.create(email=user.email)
    user = auth_user_dao.filter(email=user.email)
    new_api_key = generate_key()
    api_key_dao.create(key=new_api_key, name="", user_id=user[0][0].id)
    # TODO: remove this after migrating
    try:
        user_dao.create_users(id=user[0][0].id, credits=0)
        # Seed default Unity project, interface, tab, and table tile for tasks
        DefaultTasksSeeder.seed(session, user_id=user[0][0].id)
    except Exception as e:
        print(e)
    return {
        "id": user[0][0].id,
        "name": user[0][0].name,
        "image": user[0][0].image,
        "email": user[0][0].email,
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

    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise not_found("User ID")
    api_key = api_key_dao.filter(user_id=user[0][0].id)
    org_member = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_level = None, None
    if org_member:
        org_level = org_member[0][0].level
        org = organization_dao.filter(id=org_member[0][0].organization_id)
        org_name = org[0][0].name
    return {
        "id": user[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
        "organization": {"name": org_name, "level": org_level},
        "assistant_hiring_approval": user[0][0].assistant_hiring_approval,
        "has_claimed_approval_link": user[0][0].has_claimed_approval_link,
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

    user = auth_user_dao.filter(email=email)
    if not user:
        return None
    api_key = api_key_dao.filter(user_id=user[0][0].id)
    org_member = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_level = None, None
    if org_member:
        org_level = org_member[0][0].level
        org = organization_dao.filter(id=org_member[0][0].organization_id)
        org_name = org[0][0].name
    return {
        "id": user[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
        "organization": {"name": org_name, "level": org_level},
        "assistant_hiring_approval": user[0][0].assistant_hiring_approval,
        "has_claimed_approval_link": user[0][0].has_claimed_approval_link,
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

    account = account_dao.filter(
        provider_account_id=provider_account_id,
        provider=provider,
    )
    if not account:
        return None
    user = auth_user_dao.filter(id=account[0][0].user_id)
    if not user:
        return None
    api_key = api_key_dao.filter(user_id=user[0][0].id)
    org_member = organization_member_dao.filter(user_id=user[0][0].id)
    org_name, org_level = None, None
    if org_member:
        org_level = org_member[0][0].level
        org = organization_dao.filter(id=org_member[0][0].organization_id)
        org_name = org[0][0].name
    return {
        "id": account[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
        "organization": {"name": org_name, "level": org_level},
        "assistant_hiring_approval": user[0][0].assistant_hiring_approval,
        "has_claimed_approval_link": user[0][0].has_claimed_approval_link,
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
async def reset_user_quotas(
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


@admin_router.get("/organization/list")
async def create_organization(
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

    existing_org = organization_dao.filter(owner_id=owner_id)
    if existing_org:
        raise HTTPException(
            status_code=400,
            detail="This user already has an organization.",
        )
    organization_dao.create(name=name, owner_id=owner_id)
    new_org = organization_dao.filter(owner_id=owner_id)
    organization_member_dao.create(new_org[0][0].id, user_id=owner_id, level="owner")
    return "Organization created successfully!"


@admin_router.post("/organization/member")
async def add_organization_member(
    name: str,
    new_member_email: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)

    new_user = auth_user_dao.filter(email=new_member_email)
    if not new_user:
        raise not_found("User")
    org = organization_dao.filter(name=name)
    if not org:
        raise not_found("Organization")
    organization_member_dao.create(
        organization_id=org[0][0].id,
        user_id=new_user[0][0].id,
        level="user",
    )
    return "Member added successfully to the organization!"


@admin_router.put("/organization/member/level")
async def update_organization_member_level(
    organization: str,
    member_email: str,
    new_level: str,
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)

    if new_level not in ["user", "admin", "owner"]:
        raise HTTPException(
            status_code=400,
            detail="Level must be one of user, admin, or owner.",
        )
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
        raise not_found("User")
    organization_member_dao.update(id=org_member[0][0].id, level=new_level)
    return "Member level successfully updated!"


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
    user = auth_user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")

    current_status = user.assistant_hiring_approval
    if current_status == "approved" or current_status == "pending":
        return AssistantHiringApprovalResponse(
            message=f"Assistant hiring {current_status}.",
            assistant_hiring_approval=current_status,
        )

    if auth_user_dao.set_assistant_hiring_approval(user_id, "pending"):
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

    if auth_user_dao.set_assistant_hiring_approval(target_user_id, status):
        session.commit()
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
        description=f"Filter by status: {', '.join(s for s in ASSISTANT_HIRING_APPROVAL_STATUSES if s is not None)} or 'all'",
    ),
    session: Session = Depends(get_db_session),
):
    auth_user_dao = AuthUserDAO(session)

    users_to_list = []
    if not status_filter or status_filter.lower() == "all":
        auth_user_records = auth_user_dao.filter()
        users_to_list = [u_row[0] for u_row in auth_user_records]
    elif status_filter.lower() == "none":
        auth_user_records = auth_user_dao.filter(assistant_hiring_approval=None)
        users_to_list = [u_row[0] for u_row in auth_user_records]
    else:
        if status_filter not in ASSISTANT_HIRING_APPROVAL_STATUSES:
            valid_statuses = [
                s for s in ASSISTANT_HIRING_APPROVAL_STATUSES if s is not None
            ]
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of {', '.join(valid_statuses)}, 'none', or 'all'.",
            )
        users_to_list = auth_user_dao.get_users_by_assistant_hiring_approval(
            status_filter
        )

    return [
        AssistantHiringApprovalUserStatus(
            id=user.id,
            email=user.email,
            name=user.name,
            assistant_hiring_approval=user.assistant_hiring_approval,
            created_at=user.created_at,
        )
        for user in users_to_list
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
    user = auth_user_dao.get_by_id(user_id)
    if not user:
        raise not_found("User")
    users_dao = UsersDAO(session)

    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)
    link = token_dao.get_by_token(payload.token)
    if not link:
        raise not_found("Approval link token")

    # Check if user has claimed a link before
    if user.has_claimed_approval_link:
        message = "You have already benefited from a one-time approval link and your access is active. This link was not consumed."
        if (
            user.assistant_hiring_approval == "revoked"
            or user.assistant_hiring_approval == "rejected"
        ):
            message = "Your assistant hiring access has been re-activated using a one-time approval link. This link was not consumed, and no new credits were awarded as you have benefited previously."
        return AssistantHiringApprovalResponse(
            message=message, assistant_hiring_approval="approved"
        )

    # Check if link already used
    if link.user_id is not None:
        if link.user_id == user_id:
            return AssistantHiringApprovalResponse(
                message="You already used this approval link.",
                assistant_hiring_approval=user.assistant_hiring_approval,
            )
        else:
            raise HTTPException(
                status_code=400, detail="Approval link has already been claimed."
            )

    # Check if link expired
    if link.expires_at < datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(status_code=400, detail="Approval link has expired.")

    # Claim the link and grant free credits to the current user
    try:
        # Claim link
        claimed_link = token_dao.claim_link(payload.token, user_id)
        if not claimed_link:
            session.rollback()
            raise HTTPException(
                status_code=400,
                detail="Failed to claim approval link. It might be invalid, expired or already claimed.",
            )

        # Approve user
        if not auth_user_dao.set_assistant_hiring_approval(user.id, "approved"):
            session.rollback()
            raise HTTPException(
                status_code=500, detail="Failed to set approval status."
            )

        # Flag user as having claimed a link
        auth_user_dao.update(ide=user.id, has_claimed_approval_link=True)

        # Grant credits
        users_dao.recharge_credit(
            user_id=user.id, quantity=float(ASSISTANT_CREATION_COST)
        )

        session.commit()
        return AssistantHiringApprovalResponse(
            message="Approval link successfully claimed.",
            assistant_hiring_approval="approved",
        )
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
        days=payload.expires_in_days
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


@admin_router.delete("/assistant-hiring-one-time-link", status_code=204)
async def delete_assistant_hiring_one_time_link(
    link_id: str,
    session: Session = Depends(get_db_session),
):
    token_dao = AssistantHiringOneTimeApprovalLinkDAO(session)
    if not token_dao.delete_link(link_id):
        session.rollback()
        raise not_found("One-time approval link")
    session.commit()
    return None
