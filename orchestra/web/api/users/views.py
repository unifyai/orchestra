import datetime

from fastapi import APIRouter, Depends, HTTPException

from orchestra.db.dao.account_dao import AccountDAO
from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.web.api.users.schema import AccountRequest, SessionRequest, UserRequest

admin_router = APIRouter()


@admin_router.post("/auth-user")
async def create_user(user: UserRequest, auth_user_dao: AuthUserDAO = Depends()):
    print(user.model_dump())
    auth_user_dao.create(email=user.email)
    return ""


@admin_router.get("/auth-user/by-user-id")
async def get_user(user_id: str, auth_user_dao: AuthUserDAO = Depends()):
    user = auth_user_dao.filter(id=user_id)
    if not user:
        return None
    return user.id


@admin_router.get("/auth-user/by-email")
async def get_user_by_email(email: str, auth_user_dao: AuthUserDAO = Depends()):
    user = auth_user_dao.filter(email=email)
    if not user:
        return None
    return {
        "id": user[0][0].id,
        "email": user[0][0].email,
        # "emailVerified": None,
        # "name": "",
        # "image": "",
    }


@admin_router.get("/auth-user/by-account")
async def get_user_by_account(
    provider_account_id: str,
    provider: str,
    account_dao: AccountDAO = Depends(),
):
    account = account_dao.filter(
        provider_account_id=provider_account_id,
        provider=provider,
    )
    if not account:
        return None
    return {
        "id": account[0][0].id,
        "email": "guillermo@unify.ai",  # TODO: fetch this properly
        # "emailVerified": None,
        # "name": "",
        # "image": "",
    }


@admin_router.put("/auth-user")
async def update_user(user_id: str, updated_user: UserRequest):  # TODO
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    users_db[user_id] = updated_user
    return updated_user


@admin_router.delete("/auth-user")
async def delete_user(user_id: str):  # TODO
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    del users_db[user_id]
    return {"message": "User deleted"}


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
async def unlink_account(account: AccountRequest):  # TODO
    # Unlink an account from the user
    return {"message": f"Account {account.provider} unlinked for user {account.userId}"}


@admin_router.post("/session")
async def create_session(session: SessionRequest):  # TODO
    sessions_db[session.sessionToken] = session
    return session


@admin_router.get("/session")
async def get_session_and_user(session_token: str):  # TODO
    session = sessions_db.get(session_token)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    user = users_db.get(session.userId)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"session": session, "user": user}


@admin_router.put("/session")
async def update_session(session_token: str, updated_session: SessionRequest):  # TODO
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    sessions_db[session_token] = updated_session
    return updated_session


@admin_router.delete("/session")
async def delete_session(session_token: str):  # TODO
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    del sessions_db[session_token]
    return {"message": "Session deleted"}
