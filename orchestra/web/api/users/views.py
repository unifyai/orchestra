from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

admin_router = APIRouter()

# Dummy in-memory storage for users and sessions
users_db = {}
sessions_db = {}


class User(BaseModel):
    id: str
    email: str
    name: Optional[str] = None


class Account(BaseModel):
    provider: str
    type: str
    providerAccountId: str
    access_token: str
    expires_at: int
    scope: str
    token_type: str
    id_token: str
    userId: str


class Session(BaseModel):
    sessionToken: str
    userId: str


class CreateUserRequest(BaseModel):
    email: str
    name: Optional[str] = None


@admin_router.post("/auth-user")
async def create_user(user: CreateUserRequest):
    user_id = f"user_{len(users_db) + 1}"
    new_user = User(id=user_id, email=user.email, name=user.name)
    users_db[user_id] = new_user
    return new_user


@admin_router.get("/auth-user/by-user-id")
async def get_user(user_id: str):
    user = users_db.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@admin_router.get("/auth-user/by-email")
async def get_user_by_email(email: str):
    return None  # if there is no user with that email


@admin_router.get("/auth-user/by-account")
async def get_user_by_account(provider_account_id: str, provider: str):
    return None  # if there is no user with that account


@admin_router.put("/auth-user")
async def update_user(user_id: str, updated_user: User):
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    users_db[user_id] = updated_user
    return updated_user


@admin_router.delete("/auth-user")
async def delete_user(user_id: str):
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    del users_db[user_id]
    return {"message": "User deleted"}


@admin_router.post("/account")
async def link_account(account: Account):
    print(account)
    # Link an account to the user (e.g., social login provider)
    # TODO: this should return the newly created account
    return {"message": f"Account {account.provider} linked for user {account.userId}"}


@admin_router.delete("/account")
async def unlink_account(account: Account):
    # Unlink an account from the user
    return {"message": f"Account {account.provider} unlinked for user {account.userId}"}


@admin_router.post("/session")
async def create_session(session: Session):
    sessions_db[session.sessionToken] = session
    return session


@admin_router.get("/session")
async def get_session_and_user(session_token: str):
    session = sessions_db.get(session_token)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    user = users_db.get(session.userId)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"session": session, "user": user}


@admin_router.put("/session")
async def update_session(session_token: str, updated_session: Session):
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    sessions_db[session_token] = updated_session
    return updated_session


@admin_router.delete("/session")
async def delete_session(session_token: str):
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    del sessions_db[session_token]
    return {"message": "Session deleted"}
