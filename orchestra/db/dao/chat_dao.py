"""Data Access Object for the unified chat store.

One store backs every chat surface: human DMs, assistant DMs (the Console
1-on-1 panel), team group chats, and ad-hoc chat groups — plus first-class
call-transcript utterances. Threads are resolved get-or-create per scope;
messages append in id order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from orchestra.db.models.orchestra_models import CallUtterance, ChatMessage, ChatThread

KIND_DM = "dm"
KIND_ASSISTANT_DM = "assistant_dm"
KIND_TEAM = "team"
KIND_GROUP = "group"


def normalized_pair(user_id_1: str, user_id_2: str) -> tuple[str, str]:
    """Order a user pair so (a, b) is stable regardless of argument order."""
    return (user_id_1, user_id_2) if user_id_1 < user_id_2 else (user_id_2, user_id_1)


class ChatDAO:
    """DAO for chat threads, messages, and call utterances."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def get_thread(self, thread_id: int) -> ChatThread | None:
        return self.session.get(ChatThread, thread_id)

    def resolve_dm_thread(
        self,
        *,
        organization_id: int,
        user_id_1: str,
        user_id_2: str,
    ) -> ChatThread:
        user_a_id, user_b_id = normalized_pair(user_id_1, user_id_2)
        thread = self.session.scalar(
            select(ChatThread).where(
                ChatThread.kind == KIND_DM,
                ChatThread.organization_id == organization_id,
                ChatThread.user_a_id == user_a_id,
                ChatThread.user_b_id == user_b_id,
            ),
        )
        if thread is None:
            thread = ChatThread(
                kind=KIND_DM,
                organization_id=organization_id,
                user_a_id=user_a_id,
                user_b_id=user_b_id,
            )
            self.session.add(thread)
            self.session.flush()
        return thread

    def resolve_assistant_dm_thread(
        self,
        *,
        assistant_id: int,
        user_id: str,
        organization_id: int | None,
    ) -> ChatThread:
        thread = self.session.scalar(
            select(ChatThread).where(
                ChatThread.kind == KIND_ASSISTANT_DM,
                ChatThread.assistant_id == assistant_id,
                ChatThread.user_id == user_id,
            ),
        )
        if thread is None:
            thread = ChatThread(
                kind=KIND_ASSISTANT_DM,
                organization_id=organization_id,
                assistant_id=assistant_id,
                user_id=user_id,
            )
            self.session.add(thread)
            self.session.flush()
        return thread

    def resolve_team_thread(
        self,
        *,
        team_id: int,
        organization_id: int,
    ) -> ChatThread:
        thread = self.session.scalar(
            select(ChatThread).where(
                ChatThread.kind == KIND_TEAM,
                ChatThread.team_id == team_id,
            ),
        )
        if thread is None:
            thread = ChatThread(
                kind=KIND_TEAM,
                organization_id=organization_id,
                team_id=team_id,
            )
            self.session.add(thread)
            self.session.flush()
        return thread

    def resolve_group_thread(
        self,
        *,
        group_id: int,
        organization_id: int,
    ) -> ChatThread:
        thread = self.session.scalar(
            select(ChatThread).where(
                ChatThread.kind == KIND_GROUP,
                ChatThread.group_id == group_id,
            ),
        )
        if thread is None:
            thread = ChatThread(
                kind=KIND_GROUP,
                organization_id=organization_id,
                group_id=group_id,
            )
            self.session.add(thread)
            self.session.flush()
        return thread

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_message(
        self,
        *,
        thread: ChatThread,
        sender_user_id: str | None,
        sender_assistant_id: int | None,
        sender_name: str,
        content: str,
        mentions: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        call_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            thread_id=thread.id,
            sender_user_id=sender_user_id,
            sender_assistant_id=sender_assistant_id,
            sender_name=sender_name,
            content=content,
            mentions=mentions or [],
            attachments=attachments or [],
            call_id=call_id,
        )
        if created_at is not None:
            message.created_at = created_at
        self.session.add(message)
        self.session.flush()
        return message

    def get_message(self, *, message_id: int) -> ChatMessage | None:
        return self.session.get(ChatMessage, message_id)

    def list_messages(
        self,
        *,
        thread_id: int,
        limit: int = 100,
        before_id: int | None = None,
        q: str | None = None,
    ) -> list[ChatMessage]:
        """Most-recent-last page of messages for a thread."""
        query = select(ChatMessage).where(ChatMessage.thread_id == thread_id)
        if before_id is not None:
            query = query.where(ChatMessage.id < before_id)
        if q and q.strip():
            query = query.where(ChatMessage.content.ilike(f"%{q.strip()}%"))
        rows = self.session.scalars(
            query.order_by(ChatMessage.id.desc()).limit(limit),
        ).all()
        return list(reversed(rows))

    def search_messages(
        self,
        *,
        thread_id: int,
        q: str,
        limit: int = 50,
    ) -> list[ChatMessage]:
        """Most-recent-first content matches for a thread."""
        needle = q.strip()
        if not needle:
            return []
        query = (
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread_id,
                ChatMessage.content.ilike(f"%{needle}%"),
            )
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(query).all())

    def set_message_reactions(
        self,
        *,
        message: ChatMessage,
        reactions: list[dict[str, Any]],
    ) -> ChatMessage:
        message.reactions = reactions
        flag_modified(message, "reactions")
        self.session.flush()
        return message

    # ------------------------------------------------------------------
    # Call utterances
    # ------------------------------------------------------------------

    def add_utterance(
        self,
        *,
        call_id: str,
        reporter_assistant_id: int,
        speaker_user_id: str | None,
        speaker_assistant_id: int | None,
        speaker_name: str,
        content: str,
        spoken_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CallUtterance:
        utterance = CallUtterance(
            call_id=call_id,
            reporter_assistant_id=reporter_assistant_id,
            speaker_user_id=speaker_user_id,
            speaker_assistant_id=speaker_assistant_id,
            speaker_name=speaker_name,
            content=content,
            meta=metadata or {},
        )
        if spoken_at is not None:
            utterance.spoken_at = spoken_at
        self.session.add(utterance)
        self.session.flush()
        return utterance

    def list_calls(
        self,
        *,
        reporter_assistant_id: int,
        limit: int = 100,
    ):
        """Most-recent-last call summaries transcribed by one assistant."""
        query = (
            select(
                CallUtterance.call_id,
                func.min(CallUtterance.spoken_at).label("started_at"),
                func.max(CallUtterance.spoken_at).label("ended_at"),
                func.count().label("utterance_count"),
            )
            .where(CallUtterance.reporter_assistant_id == reporter_assistant_id)
            .group_by(CallUtterance.call_id)
            .order_by(func.max(CallUtterance.spoken_at).asc())
            .limit(limit)
        )
        return self.session.execute(query).all()

    def list_utterances(
        self,
        *,
        call_id: str,
        reporter_assistant_id: int | None = None,
        limit: int = 1000,
        before_id: int | None = None,
    ) -> list[CallUtterance]:
        """Oldest-first page of utterances for a call.

        When ``reporter_assistant_id`` is omitted, the reporter with the
        lowest assistant id is selected so multi-assistant calls yield one
        coherent transcript rather than interleaved duplicates.
        """
        if reporter_assistant_id is None:
            reporter_assistant_id = self.session.scalar(
                select(func.min(CallUtterance.reporter_assistant_id)).where(
                    CallUtterance.call_id == call_id,
                ),
            )
            if reporter_assistant_id is None:
                return []
        query = select(CallUtterance).where(
            CallUtterance.call_id == call_id,
            CallUtterance.reporter_assistant_id == reporter_assistant_id,
        )
        if before_id is not None:
            query = query.where(CallUtterance.id < before_id)
        rows = self.session.scalars(
            query.order_by(CallUtterance.id.desc()).limit(limit),
        ).all()
        return list(reversed(rows))
