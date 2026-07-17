"""Schemas for org roster, team group chat, human DMs, and call sessions."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class RosterHuman(BaseModel):
    """One human org member with profile and presence."""

    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None
    role_name: Optional[str] = None
    bio: Optional[str] = None
    job_title: Optional[str] = None
    phone_number: Optional[str] = None
    whatsapp_number: Optional[str] = None
    timezone: Optional[str] = None
    online: bool = False
    last_seen_at: Optional[datetime] = None


class RosterTeam(BaseModel):
    """One team with human members and non-coordinator assistant members."""

    team_id: int
    name: str
    description: Optional[str] = None
    is_org_wide_sharing: bool = False
    created_at: Optional[datetime] = None
    member_user_ids: List[str] = Field(default_factory=list)
    assistant_member_ids: List[int] = Field(default_factory=list)
    image: Optional[str] = None


class RosterGroup(BaseModel):
    """One chat group the caller belongs to (humans + assistants)."""

    group_id: int
    name: str
    created_by_user_id: str
    created_at: Optional[datetime] = None
    member_user_ids: List[str] = Field(default_factory=list)
    assistant_member_ids: List[int] = Field(default_factory=list)


class OrgRosterResponse(BaseModel):
    """Everything the Console selector needs beyond the assistant list."""

    organization_id: int
    humans: List[RosterHuman]
    teams: List[RosterTeam]
    groups: List[RosterGroup] = Field(default_factory=list)


class ChatMention(BaseModel):
    """One @mention inside a chat message."""

    kind: Literal["user", "assistant"]
    id: str
    name: Optional[str] = None


class OrgChatAttachment(BaseModel):
    """One attachment referenced by an org-chat message."""

    id: str
    filename: str
    gs_url: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    signed_url: Optional[str] = None


class OrgChatMessageCreate(BaseModel):
    """Shared body fields for org-chat message creation."""

    content: str = Field(default="", max_length=20000)
    attachments: List[OrgChatAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message_body(self):
        if self.content.strip() or self.attachments:
            return self
        raise ValueError("Either content or attachments is required.")


class OrgChatReaction(BaseModel):
    """One emoji reaction on an org-chat message."""

    user_id: str
    emoji: str
    updated_at: Optional[str] = None


class ChatGroupCreate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    user_ids: List[str] = Field(default_factory=list)
    assistant_ids: List[int] = Field(default_factory=list)


class ChatGroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    user_ids: Optional[List[str]] = None
    assistant_ids: Optional[List[int]] = None


class ChatGroupResponse(BaseModel):
    group_id: int
    name: str
    organization_id: int
    created_by_user_id: str
    created_at: Optional[datetime] = None
    member_user_ids: List[str] = Field(default_factory=list)
    assistant_member_ids: List[int] = Field(default_factory=list)


class ChatGroupsPage(BaseModel):
    groups: List[ChatGroupResponse]


OrgCallStatus = Literal["ringing", "active", "ended"]
OrgCallScope = Literal["dm", "team", "group"]
OrgCallParticipantStatus = Literal["invited", "joined", "declined", "left"]
OrgCallParticipantRole = Literal["host", "member"]


class OrgCallParticipantResponse(BaseModel):
    user_id: str
    role: OrgCallParticipantRole
    status: OrgCallParticipantStatus


class OrgCallRosterMember(BaseModel):
    kind: Literal["human", "assistant"]
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    display_name: str
    contact_id: Optional[int] = None
    email: Optional[str] = None


class OrgCallSessionResponse(BaseModel):
    call_id: str
    room_name: str
    status: OrgCallStatus
    scope: OrgCallScope
    created_by_user_id: str
    caller_user_id: str
    callee_user_id: str | None = None
    team_id: int | None = None
    group_id: int | None = None
    thread_id: int | None = None
    dm_thread_id: int | None = None
    user_ids: List[str] = Field(default_factory=list)
    participants: List[OrgCallParticipantResponse] = Field(default_factory=list)
    assistant_ids: List[int] = Field(default_factory=list)
    roster: List[OrgCallRosterMember] = Field(default_factory=list)


class OrgCallCreateResponse(OrgCallSessionResponse):
    pass


class OrgCallAddAssistantRequest(BaseModel):
    assistant_id: int


# Backward-compatible aliases used by older clients / tests.
HumanCallStatus = OrgCallStatus
HumanCallSessionResponse = OrgCallSessionResponse
HumanCallCreateResponse = OrgCallCreateResponse
