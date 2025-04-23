from typing import Optional

from pydantic import BaseModel, ConfigDict


class UserRequest(BaseModel):
    email: Optional[str] = None
    user_id: Optional[str] = None
    image: Optional[str] = None
    name: Optional[str] = None
    last_name: Optional[str] = None
    job_title: Optional[str] = None


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


class FreezeAccountRequest(BaseModel):
    user_id: str
    freeze: bool = True


class QueryLoggingStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool


class UpdateQueryLoggingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool
