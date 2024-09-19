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
    providerAccountId: str
    userId: str


class Session(BaseModel):
    sessionToken: str
    userId: str


class CreateUserRequest(BaseModel):
    email: str
    name: Optional[str] = None


@admin_router.post("/create-user")
async def create_user(user: CreateUserRequest):
    user_id = f"user_{len(users_db) + 1}"
    new_user = User(id=user_id, email=user.email, name=user.name)
    users_db[user_id] = new_user
    return new_user


@admin_router.get("/user/{user_id}")
async def get_user(user_id: str):
    user = users_db.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@admin_router.get("/user-by-email/{email}")
async def get_user_by_email(email: str):
    user = next((user for user in users_db.values() if user.email == email), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@admin_router.post("/user-by-account")
async def get_user_by_account(account: Account):
    user = next((user for user in users_db.values() if user.id == account.userId), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@admin_router.put("/update-user/{user_id}")
async def update_user(user_id: str, updated_user: User):
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    users_db[user_id] = updated_user
    return updated_user


@admin_router.delete("/delete-user/{user_id}")
async def delete_user(user_id: str):
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    del users_db[user_id]
    return {"message": "User deleted"}


@admin_router.post("/link-account")
async def link_account(account: Account):
    # Link an account to the user (e.g., social login provider)
    return {"message": f"Account {account.provider} linked for user {account.userId}"}


@admin_router.post("/unlink-account")
async def unlink_account(account: Account):
    # Unlink an account from the user
    return {"message": f"Account {account.provider} unlinked for user {account.userId}"}


@admin_router.post("/create-session")
async def create_session(session: Session):
    sessions_db[session.sessionToken] = session
    return session


@admin_router.get("/session/{session_token}")
async def get_session_and_user(session_token: str):
    session = sessions_db.get(session_token)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    user = users_db.get(session.userId)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"session": session, "user": user}


@admin_router.put("/update-session/{session_token}")
async def update_session(session_token: str, updated_session: Session):
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    sessions_db[session_token] = updated_session
    return updated_session


@admin_router.delete("/delete-session/{session_token}")
async def delete_session(session_token: str):
    if session_token not in sessions_db:
        raise HTTPException(status_code=404, detail="Session not found")
    del sessions_db[session_token]
    return {"message": "Session deleted"}
