"""Schemas for org roster, team group chat, and human DMs."""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


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


class OrgRosterResponse(BaseModel):
    """Everything the Console selector needs beyond the assistant list."""

    organization_id: int
    humans: List[RosterHuman]
    teams: List[RosterTeam]


class ChatMention(BaseModel):
    """One @mention inside a chat message."""

    kind: Literal["user", "assistant"]
    id: str
    name: Optional[str] = None


class TeamMessageCreate(BaseModel):
    """Body for a human posting to a team group chat."""

    content: str = Field(min_length=1, max_length=20000)
    mentions: List[ChatMention] = Field(default_factory=list)


class AssistantTeamMessageCreate(BaseModel):
    """Body for an assistant runtime posting a group-chat reply (admin auth)."""

    assistant_id: int
    content: str = Field(min_length=1, max_length=20000)
    mentions: List[ChatMention] = Field(default_factory=list)


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


class TeamMessagesPage(BaseModel):
    messages: List[TeamMessageResponse]


class DmMessageCreate(BaseModel):
    """Body for sending a DM to another org member."""

    content: str = Field(min_length=1, max_length=20000)


class DmMessageResponse(BaseModel):
    """One stored DM message."""

    id: int
    thread_id: int
    sender_user_id: Optional[str] = None
    content: str
    created_at: datetime


class DmMessagesPage(BaseModel):
    thread_id: int
    organization_id: int
    user_ids: List[str]
    messages: List[DmMessageResponse]
