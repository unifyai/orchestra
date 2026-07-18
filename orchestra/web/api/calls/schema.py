"""Schemas for the unified call session API.

Calls are thread-scoped like chat: a call is created against one conversation
scope (human DM, team, group, or 1:1 assistant DM) and binds to that scope's
``chat_thread``. Humans are participants; assistants ride ``assistant_ids``
and are dispatched into the LiveKit room server-side.
"""

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

CallScopeKind = Literal["dm", "team", "group", "assistant_dm"]
CallStatus = Literal["ringing", "active", "ended"]
CallParticipantStatus = Literal["invited", "joined", "declined", "left"]


class CallCreate(BaseModel):
    """Create a call against one chat scope the caller belongs to."""

    kind: CallScopeKind
    organization_id: Optional[int] = None
    peer_user_id: Optional[str] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None
    assistant_id: Optional[int] = None
    # Voice-agent opening behavior forwarded on assistant dispatch.
    opening_config: Optional[dict[str, Any]] = None


class AssistantCallCreate(BaseModel):
    """Assistant-initiated ring (runtime, admin auth): ring the thread human."""

    assistant_id: int
    user_id: Optional[str] = None
    opening_config: Optional[dict[str, Any]] = None


class OwnedAssistantCallCreate(BaseModel):
    """Assistant-initiated ring via the owner-scoped route."""

    user_id: Optional[str] = None
    opening_config: Optional[dict[str, Any]] = None


class CallParticipantResponse(BaseModel):
    user_id: str
    role: Literal["host", "member"]
    status: CallParticipantStatus


class CallRosterMember(BaseModel):
    kind: Literal["human", "assistant"]
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    display_name: str
    contact_id: Optional[int] = None
    email: Optional[str] = None


class CallSessionResponse(BaseModel):
    call_id: str
    room_name: str
    status: CallStatus
    scope: CallScopeKind
    organization_id: Optional[int] = None
    thread_id: Optional[int] = None
    created_by_user_id: str
    created_by_assistant_id: Optional[int] = None
    caller_user_id: str
    callee_user_id: Optional[str] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None
    user_ids: List[str] = Field(default_factory=list)
    participants: List[CallParticipantResponse] = Field(default_factory=list)
    assistant_ids: List[int] = Field(default_factory=list)
    roster: List[CallRosterMember] = Field(default_factory=list)


class CallCreateResponse(CallSessionResponse):
    pass


class CallsActiveResponse(BaseModel):
    """Live (ringing/active) call sessions that include the caller."""

    calls: List[CallSessionResponse] = Field(default_factory=list)


class CallAddAssistantRequest(BaseModel):
    assistant_id: int
