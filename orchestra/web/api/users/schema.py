from typing import Optional

from pydantic import BaseModel


class UserRequest(BaseModel):
    email: str
    image: Optional[str] = None
    emailVerified: Optional[str] = None
    name: Optional[str] = None


class AccountRequest(BaseModel):
    provider: str
    type: str
    providerAccountId: str
    access_token: str
    expires_at: int
    scope: str
    token_type: str
    id_token: str
    userId: Optional[str] = None


class SessionRequest(BaseModel):
    placeholder: str
