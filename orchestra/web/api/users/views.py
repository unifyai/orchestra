import base64
import datetime
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.users.schema import AccountRequest, UserRequest

admin_router = APIRouter()

# TODO: Move exceptions to exceptions file
# TODO: Fetch organization if it exists when reading user info
# TODO: Return tier in user info endpoints + double check rest of the information

# Endpoints used by next-auth


@admin_router.post("/auth-user")
async def create_user(
    user: UserRequest,
    auth_user_dao: AuthUserDAO = Depends(),
    api_key_dao: ApiKeyDAO = Depends(),
    user_dao: UsersDAO = Depends(),
):
    auth_user_dao.create(email=user.email)
    user = auth_user_dao.filter(email=user.email)
    new_api_key = generate_key()
    api_key_dao.create(key=new_api_key, name="", user_id=user[0][0].id)
    # TODO: remove this after migrating
    try:
        user_dao.create_users(id=user[0][0].id, credits=0)
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
    auth_user_dao: AuthUserDAO = Depends(),
    api_key_dao: ApiKeyDAO = Depends(),
):
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User ID Not Found.")
        # TODO: check that return None can be remoed here fine
    api_key = api_key_dao.filter(user_id=user[0][0].id)
    return {
        "id": user[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
    }


@admin_router.get("/auth-user/by-email")
async def get_user_by_email(
    email: str,
    auth_user_dao: AuthUserDAO = Depends(),
    api_key_dao: ApiKeyDAO = Depends(),
):
    user = auth_user_dao.filter(email=email)
    if not user:
        return None
    api_key = api_key_dao.filter(user_id=user[0][0].id)
    return {
        "id": user[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
    }


@admin_router.get("/auth-user/by-account")
async def get_user_by_account(
    provider_account_id: str,
    provider: str,
    account_dao: AccountDAO = Depends(),
    auth_user_dao: AuthUserDAO = Depends(),
    api_key_dao: ApiKeyDAO = Depends(),
):
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
    return {
        "id": account[0][0].id,
        "name": user[0][0].name,
        "lastName": user[0][0].last_name,
        "jobTitle": user[0][0].job_title,
        "image": user[0][0].image,
        "email": user[0][0].email,
        "createdAt": user[0][0].created_at,
        "apiKey": api_key[0][0].key,
    }


@admin_router.put("/auth-user")
async def update_user(
    updated_user: UserRequest,
    auth_user_dao: AuthUserDAO = Depends(),
):
    user = auth_user_dao.filter(id=updated_user.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    auth_user_dao.update(
        id=updated_user.user_id,
        name=updated_user.name,
        last_name=updated_user.last_name,
        job_title=updated_user.job_title,
    )
    return "User information updated successfully!"


@admin_router.delete("/auth-user")
async def delete_user(user_id: str, auth_user_dao: AuthUserDAO = Depends()):
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    auth_user_dao.delete(id=user_id)
    return "User deleted successfully!"


@admin_router.post("/account")
async def link_account(account: AccountRequest, account_dao: AccountDAO = Depends()):
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
    auth_user_dao: AuthUserDAO = Depends(),
):
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User ID Not Found.")
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
    auth_user_dao: AuthUserDAO = Depends(),
):
    user = auth_user_dao.filter(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User ID Not Found.")
    auth_user_dao.update(id=user_id, queries_enabled=True, evaluations_enabled=True)
    return "User quotas reset successfully!"


@admin_router.get("/api_key/list")
async def list_user_api_keys(
    user_id: str,
    api_key_dao: ApiKeyDAO = Depends(),
):
    keys = api_key_dao.filter(user_id=user_id)
    if not keys:
        raise HTTPException(
            status_code=404,
            detail=f"API Keys not found for user {user_id}",
        )
    return keys


@admin_router.post("/api_key")
async def create_api_key(
    name: str,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    api_key_dao: ApiKeyDAO = Depends(),
):
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
    api_key_dao: ApiKeyDAO = Depends(),
):
    # TODO: This deletes all previous key from a user/org and creates a new one,
    # this will need to be changed once multiple keys are enabled.
    # delete prev key
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


@admin_router.get("/organization/list")  # TODO
async def create_organization(
    name: str,
    organization_dao: OrganizationDAO = Depends(),
    organization_member_dao: OrganizationMemberDAO = Depends(),
):
    # TODO
    return "TODO"


@admin_router.post("/organization")
async def create_organization(
    name: str,
    owner_id: Optional[str] = None,
    organization_dao: OrganizationDAO = Depends(),
    organization_member_dao: OrganizationMemberDAO = Depends(),
):
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
    auth_user_dao: AuthUserDAO = Depends(),
    organization_dao: OrganizationDAO = Depends(),
    organization_member_dao: OrganizationMemberDAO = Depends(),
):
    new_user = auth_user_dao.filter(email=new_member_email)
    if not new_user:
        raise HTTPException(status_code=404, detail="User not found.")
    org = organization_dao.filter(name=name)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")
    organization_member_dao.create(
        organization_id=org[0][0].id,
        user_id=new_user[0][0].id,
        level="user",
    )
    return "Member added successfully to the organization!"
