from typing import Optional

from pydantic import BaseModel, ConfigDict

from datetime import datetime

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

# -- Assistant hiring approval --
class AssistantHiringApprovalResponse(BaseModel):
    message: str
    assistant_hiring_approval: Optional[str]

class AssistantHiringOneTimeLinkClaimTokenRequest(BaseModel):
    token: str

class AssistantHiringApprovalUserStatus(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    assistant_hiring_approval: Optional[str]
    created_at: datetime

class AssistantHiringApprovalCreateLinkRequest(BaseModel):
    expires_in_days: int = 7

class AssistantHiringOneTimeLinkResponse(BaseModel):
    id: str
    token: str
    expires_at: datetime
    claimed_at: Optional[datetime] = None
    user_id: Optional[str] = None
