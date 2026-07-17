"""Schemas for the unified chat API."""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from orchestra.web.api.org_chat.schema import (
    ChatMention,
    OrgChatAttachment,
    OrgChatReaction,
)

ChatThreadKind = Literal["dm", "assistant_dm", "team", "group"]


class ChatThreadResolve(BaseModel):
    """Get-or-create one thread by scope.

    Exactly the scope fields for ``kind`` are required: ``peer_user_id`` (+
    ``organization_id``) for ``dm``, ``assistant_id`` for ``assistant_dm``,
    ``team_id`` for ``team``, ``group_id`` for ``group``.
    """

    kind: ChatThreadKind
    organization_id: Optional[int] = None
    peer_user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None


class ChatThreadResponse(BaseModel):
    thread_id: int
    kind: ChatThreadKind
    organization_id: Optional[int] = None
    user_ids: List[str] = Field(default_factory=list)
    assistant_id: Optional[int] = None
    user_id: Optional[str] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None


class ChatMessageCreate(BaseModel):
    content: str = Field(default="", max_length=20000)
    mentions: List[ChatMention] = Field(default_factory=list)
    attachments: List[OrgChatAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message_body(self):
        if self.content.strip() or self.attachments:
            return self
        raise ValueError("Either content or attachments is required.")


class AssistantChatMessageCreate(ChatMessageCreate):
    """Assistant-runtime send: resolves the target thread in one call.

    Precedence: ``thread_id`` > ``group_id`` > ``team_id`` > assistant DM
    with ``to_user_id`` (defaulting to the assistant's owner).
    """

    thread_id: Optional[int] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None
    to_user_id: Optional[str] = None


class AdminAssistantChatMessageCreate(AssistantChatMessageCreate):
    assistant_id: int


class ChatMessageResponse(BaseModel):
    id: int
    thread_id: int
    kind: ChatThreadKind
    organization_id: Optional[int] = None
    user_ids: List[str] = Field(default_factory=list)
    assistant_id: Optional[int] = None
    user_id: Optional[str] = None
    team_id: Optional[int] = None
    group_id: Optional[int] = None
    sender_kind: Literal["user", "assistant"]
    sender_user_id: Optional[str] = None
    sender_assistant_id: Optional[int] = None
    sender_name: str = ""
    content: str = ""
    mentions: List[dict[str, Any]] = Field(default_factory=list)
    attachments: List[OrgChatAttachment] = Field(default_factory=list)
    reactions: List[OrgChatReaction] = Field(default_factory=list)
    call_id: Optional[str] = None
    timestamp: Optional[str] = None


class ChatMessagesPage(BaseModel):
    thread: ChatThreadResponse
    messages: List[ChatMessageResponse]


class ChatReactionUpdate(BaseModel):
    """Toggle body: set an emoji, or null/empty to clear the caller's reaction."""

    emoji: Optional[str] = Field(default=None, max_length=32)


class CallUtteranceIn(BaseModel):
    content: str
    speaker_name: str = ""
    speaker_user_id: Optional[str] = None
    speaker_assistant_id: Optional[int] = None
    spoken_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CallUtterancesCreate(BaseModel):
    call_id: str
    utterances: List[CallUtteranceIn]


class AdminCallUtterancesCreate(CallUtterancesCreate):
    assistant_id: int


class CallUtteranceResponse(BaseModel):
    id: int
    call_id: str
    reporter_assistant_id: int
    speaker_user_id: Optional[str] = None
    speaker_assistant_id: Optional[int] = None
    speaker_name: str = ""
    content: str
    spoken_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CallUtterancesPage(BaseModel):
    call_id: str
    utterances: List[CallUtteranceResponse]


class CallSummaryResponse(BaseModel):
    call_id: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    utterance_count: int = 0


class CallsPage(BaseModel):
    calls: List[CallSummaryResponse]
