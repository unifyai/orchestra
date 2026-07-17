"""Schemas for org roster, team group chat, human DMs, and call sessions."""

from datetime import datetime
from typing import Any, List, Literal, Optional

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


class TeamMessageCreate(OrgChatMessageCreate):
    """Body for a human posting to a team group chat."""

    mentions: List[ChatMention] = Field(default_factory=list)


class AssistantTeamMessageCreate(OrgChatMessageCreate):
    """Body for an assistant runtime posting a group-chat reply (admin auth)."""

    assistant_id: int
    mentions: List[ChatMention] = Field(default_factory=list)


class OrgChatReaction(BaseModel):
    """One emoji reaction on an org-chat message."""

    user_id: str
    emoji: str
    updated_at: Optional[str] = None


class OrgChatReactionUpdate(BaseModel):
    """Toggle body: set an emoji, or null/empty to clear the caller's reaction."""

    emoji: Optional[str] = Field(default=None, max_length=32)


class TeamMessageResponse(BaseModel):
    """One stored team group-chat message."""

    message_id: int
    team_id: int
    organization_id: int
    timestamp: str
    sender_kind: Literal["user", "assistant"]
    sender_user_id: Optional[str] = None
    sender_assistant_id: Optional[int] = None
    sender_name: str
    content: str
    mentions: List[dict[str, Any]] = Field(default_factory=list)
    attachments: List[OrgChatAttachment] = Field(default_factory=list)
    reactions: List[OrgChatReaction] = Field(default_factory=list)


class TeamMessagesPage(BaseModel):
    messages: List[TeamMessageResponse]


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


class GroupMessageCreate(OrgChatMessageCreate):
    mentions: List[ChatMention] = Field(default_factory=list)


class AssistantGroupMessageCreate(OrgChatMessageCreate):
    assistant_id: int
    mentions: List[ChatMention] = Field(default_factory=list)


class GroupMessageResponse(BaseModel):
    message_id: int
    group_id: int
    organization_id: int
    timestamp: str
    sender_kind: Literal["user", "assistant"]
    sender_user_id: Optional[str] = None
    sender_assistant_id: Optional[int] = None
    sender_name: str
    content: str
    mentions: List[dict[str, Any]] = Field(default_factory=list)
    attachments: List[OrgChatAttachment] = Field(default_factory=list)
    reactions: List[OrgChatReaction] = Field(default_factory=list)


class GroupMessagesPage(BaseModel):
    messages: List[GroupMessageResponse]


class DmMessageCreate(OrgChatMessageCreate):
    """Body for sending a DM to another org member."""


class DmMessageResponse(BaseModel):
    """One stored DM message."""

    id: int
    thread_id: int
    sender_user_id: Optional[str] = None
    content: str
    created_at: datetime
    attachments: List[OrgChatAttachment] = Field(default_factory=list)
    reactions: List[OrgChatReaction] = Field(default_factory=list)


class DmMessagesPage(BaseModel):
    thread_id: int
    organization_id: int
    user_ids: List[str]
    messages: List[DmMessageResponse]


class OrgChatSearchResult(BaseModel):
    id: str
    scope: Literal["dm", "team", "group"]
    content: str
    timestamp: Optional[str] = None
    sender_name: str


class OrgChatSearchPage(BaseModel):
    results: List[OrgChatSearchResult]


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
