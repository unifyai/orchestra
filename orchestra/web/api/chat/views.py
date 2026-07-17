"""Unified chat endpoints: threads, messages, reactions, call utterances.

Every Console chat surface reads and writes here. Message persistence is the
Postgres-backed unified store; realtime delivery and assistant fan-out are
delegated to the hosted communication layer (adapters ``POST /unify/chat``)
after commit, best-effort. Assistant runtimes post through the
``/assistant/{agent_id}/chat/messages`` (ownership auth) or
``/admin/chat/messages`` (admin auth) routes and never write Console history
into Transcripts.
"""

import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.chat_dao import KIND_ASSISTANT_DM, KIND_DM, KIND_TEAM, ChatDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Assistant,
    ChatGroup,
    ChatThread,
    Organization,
    Team,
    User,
)
from orchestra.services.bucket_service import create_bucket_service
from orchestra.services.chat_service import (
    apply_user_reaction,
    assistant_can_access_thread,
    assistant_display_name,
    build_chat_dispatch_payload,
    human_can_access_thread,
    message_payload,
    thread_summary,
    user_display_name,
)
from orchestra.services.org_chat_service import assistant_email
from orchestra.web.api.chat.schema import (
    AdminAssistantChatMessageCreate,
    AdminCallUtterancesCreate,
    AssistantChatMessageCreate,
    CallsPage,
    CallSummaryResponse,
    CallUtteranceResponse,
    CallUtterancesCreate,
    CallUtterancesPage,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatMessagesPage,
    ChatReactionUpdate,
    ChatThreadResolve,
    ChatThreadResponse,
)
from orchestra.web.api.org_chat.schema import OrgChatAttachment
from orchestra.web.api.utils.assistant_infra import dispatch_chat_best_effort
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant
from orchestra.web.api.utils.gcp import parse_gcs_url

router = APIRouter()
admin_router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def _generate_signed_url(gs_url: str) -> str | None:
    """Best-effort signed URL generation for a gs:// URI."""
    try:
        bucket_name, object_path = parse_gcs_url(gs_url)
        if not bucket_name or not object_path:
            return None
        svc = create_bucket_service()
        bucket = svc.storage_client.bucket(bucket_name)
        blob = bucket.blob(object_path)
        if not blob.exists():
            return None
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(hours=1),
            method="GET",
        )
    except Exception:
        logger.debug("Failed to generate signed URL for %s", gs_url, exc_info=True)
        return None


def enrich_attachments(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Add fresh signed URLs to stored attachment dicts."""
    if not raw:
        return []
    enriched = []
    for attachment in raw:
        item = dict(attachment)
        gs_url = item.get("gs_url")
        if gs_url:
            signed = _generate_signed_url(gs_url)
            if signed:
                item["signed_url"] = signed
        enriched.append(item)
    return enriched


def attachments_for_storage(
    attachments: list[OrgChatAttachment],
) -> list[dict[str, Any]]:
    return [
        attachment.model_dump(
            exclude_none=True,
            exclude={"signed_url"},
        )
        for attachment in attachments
    ]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_org(session: Session, organization_id: int) -> Organization:
    org = OrganizationDAO(session).get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )
    return org


def _require_org_member(
    session: Session,
    *,
    org: Organization,
    user_id: str,
) -> None:
    is_owner = org.owner_id == user_id
    is_member = OrganizationMemberDAO(session).filter(
        user_id=user_id,
        organization_id=org.id,
    )
    if not is_owner and not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this organization",
        )


def _require_thread(session: Session, thread_id: int) -> ChatThread:
    thread = session.get(ChatThread, thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chat thread with id {thread_id} not found",
        )
    return thread


def _require_human_thread_access(
    session: Session,
    *,
    thread: ChatThread,
    user_id: str,
) -> None:
    if not human_can_access_thread(session, thread=thread, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this chat thread",
        )


def _message_response(payload: dict[str, Any]) -> ChatMessageResponse:
    enriched = dict(payload)
    enriched["attachments"] = enrich_attachments(enriched.get("attachments"))
    enriched["reactions"] = [
        {
            "user_id": str(item.get("user_id")),
            "emoji": str(item.get("emoji")),
            **(
                {"updated_at": item.get("updated_at")}
                if isinstance(item.get("updated_at"), str)
                else {}
            ),
        }
        for item in (enriched.get("reactions") or [])
        if isinstance(item, dict) and item.get("user_id") and item.get("emoji")
    ]
    return ChatMessageResponse(**enriched)


def _thread_response(thread: ChatThread) -> ChatThreadResponse:
    summary = thread_summary(thread)
    summary["user_ids"] = [uid for uid in summary["user_ids"] if uid]
    return ChatThreadResponse(**summary)


# ---------------------------------------------------------------------------
# Thread resolution
# ---------------------------------------------------------------------------


@router.post("/chat/threads/resolve", response_model=ChatThreadResponse)
def resolve_chat_thread(
    request_fastapi: Request,
    body: ChatThreadResolve,
    session: Session = Depends(get_db_session),
) -> ChatThreadResponse:
    """Get-or-create the thread for one chat scope the caller belongs to."""
    user_id = request_fastapi.state.user_id
    dao = ChatDAO(session)

    if body.kind == KIND_DM:
        if body.organization_id is None or not body.peer_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dm threads require organization_id and peer_user_id",
            )
        if body.peer_user_id == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot open a DM with yourself",
            )
        org = _require_org(session, body.organization_id)
        _require_org_member(session, org=org, user_id=user_id)
        peer = session.get(User, body.peer_user_id)
        if peer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        _require_org_member(session, org=org, user_id=body.peer_user_id)
        thread = dao.resolve_dm_thread(
            organization_id=body.organization_id,
            user_id_1=user_id,
            user_id_2=body.peer_user_id,
        )
    elif body.kind == KIND_ASSISTANT_DM:
        if body.assistant_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assistant_dm threads require assistant_id",
            )
        assistant = require_owned_assistant(
            request_fastapi,
            body.assistant_id,
            session,
        )
        thread = dao.resolve_assistant_dm_thread(
            assistant_id=assistant.agent_id,
            user_id=user_id,
            organization_id=assistant.organization_id,
        )
    elif body.kind == KIND_TEAM:
        if body.team_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="team threads require team_id",
            )
        team = session.get(Team, body.team_id)
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )
        if not TeamDAO(session).is_team_member(team.id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of this team",
            )
        thread = dao.resolve_team_thread(
            team_id=team.id,
            organization_id=team.organization_id,
        )
    else:
        if body.group_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="group threads require group_id",
            )
        group = session.scalar(
            select(ChatGroup).where(
                ChatGroup.id == body.group_id,
                ChatGroup.status == "active",
            ),
        )
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat group not found",
            )
        from orchestra.services.chat_group_service import is_human_group_member

        if not is_human_group_member(
            session,
            group_id=group.id,
            user_id=user_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of this group",
            )
        thread = dao.resolve_group_thread(
            group_id=group.id,
            organization_id=group.organization_id,
        )

    session.commit()
    return _thread_response(thread)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@router.get(
    "/chat/threads/{thread_id}/messages",
    response_model=ChatMessagesPage,
)
def get_chat_messages(
    request_fastapi: Request,
    thread_id: int,
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None, ge=0),
    q: str | None = Query(None, min_length=1, max_length=200),
    session: Session = Depends(get_db_session),
) -> ChatMessagesPage:
    """History for one thread (most recent last)."""
    user_id = request_fastapi.state.user_id
    thread = _require_thread(session, thread_id)
    _require_human_thread_access(session, thread=thread, user_id=user_id)
    messages = ChatDAO(session).list_messages(
        thread_id=thread.id,
        limit=limit,
        before_id=before_id,
        q=q,
    )
    return ChatMessagesPage(
        thread=_thread_response(thread),
        messages=[
            _message_response(message_payload(message, thread)) for message in messages
        ],
    )


@router.get(
    "/chat/threads/{thread_id}/search",
    response_model=ChatMessagesPage,
)
def search_chat_messages(
    request_fastapi: Request,
    thread_id: int,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> ChatMessagesPage:
    """Most-recent-first content matches inside one thread."""
    user_id = request_fastapi.state.user_id
    thread = _require_thread(session, thread_id)
    _require_human_thread_access(session, thread=thread, user_id=user_id)
    matches = ChatDAO(session).search_messages(
        thread_id=thread.id,
        q=q,
        limit=limit,
    )
    return ChatMessagesPage(
        thread=_thread_response(thread),
        messages=[
            _message_response(message_payload(message, thread)) for message in matches
        ],
    )


@router.post(
    "/chat/threads/{thread_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_chat_message(
    request_fastapi: Request,
    thread_id: int,
    body: ChatMessageCreate,
    session: Session = Depends(get_db_session),
) -> ChatMessageResponse:
    """Post one message as the authenticated human.

    Persists the message, then hands realtime delivery + assistant fan-out
    to the hosted communication layer (best-effort: a hosted hiccup does not
    fail the accepted message).
    """
    user_id = request_fastapi.state.user_id
    thread = _require_thread(session, thread_id)
    _require_human_thread_access(session, thread=thread, user_id=user_id)

    sender = session.get(User, user_id)
    if sender is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    message = ChatDAO(session).add_message(
        thread=thread,
        sender_user_id=user_id,
        sender_assistant_id=None,
        sender_name=user_display_name(sender),
        content=body.content,
        mentions=[mention.model_dump() for mention in body.mentions],
        attachments=attachments_for_storage(body.attachments),
    )
    session.commit()

    response = _message_response(message_payload(message, thread))
    await dispatch_chat_best_effort(
        build_chat_dispatch_payload(
            session,
            thread=thread,
            message=response.model_dump(),
            sender_email=sender.email or "",
        ),
    )
    return response


async def _post_assistant_chat_message(
    session: Session,
    *,
    assistant: Assistant,
    body: AssistantChatMessageCreate,
) -> ChatMessageResponse:
    """Persist and dispatch one assistant-authored message.

    The reply is persisted, published to the Console stream, and fanned out
    to every other member assistant (the author is excluded — it already
    knows what it said).
    """
    dao = ChatDAO(session)
    if body.thread_id is not None:
        thread = _require_thread(session, body.thread_id)
    elif body.group_id is not None:
        group = session.scalar(
            select(ChatGroup).where(
                ChatGroup.id == body.group_id,
                ChatGroup.status == "active",
            ),
        )
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat group not found",
            )
        thread = dao.resolve_group_thread(
            group_id=group.id,
            organization_id=group.organization_id,
        )
    elif body.team_id is not None:
        team = session.get(Team, body.team_id)
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )
        thread = dao.resolve_team_thread(
            team_id=team.id,
            organization_id=team.organization_id,
        )
    else:
        to_user_id = body.to_user_id or assistant.user_id
        if not to_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Assistant has no owner to message",
            )
        thread = dao.resolve_assistant_dm_thread(
            assistant_id=assistant.agent_id,
            user_id=to_user_id,
            organization_id=assistant.organization_id,
        )

    if not assistant_can_access_thread(
        session,
        thread=thread,
        assistant_id=assistant.agent_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assistant is not a member of this chat thread",
        )

    message = dao.add_message(
        thread=thread,
        sender_user_id=None,
        sender_assistant_id=assistant.agent_id,
        sender_name=assistant_display_name(assistant),
        content=body.content,
        mentions=[mention.model_dump() for mention in body.mentions],
        attachments=attachments_for_storage(body.attachments),
    )
    session.commit()

    response = _message_response(message_payload(message, thread))
    await dispatch_chat_best_effort(
        build_chat_dispatch_payload(
            session,
            thread=thread,
            message=response.model_dump(),
            sender_email=assistant_email(session, assistant.agent_id),
            exclude_assistant_id=assistant.agent_id,
        ),
    )
    return response


@router.post(
    "/assistant/{agent_id}/chat/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_chat_message_as_owned_assistant(
    request_fastapi: Request,
    agent_id: int,
    body: AssistantChatMessageCreate,
    session: Session = Depends(get_db_session),
) -> ChatMessageResponse:
    """Assistant runtime posting a message (ownership-scoped auth)."""
    assistant = require_owned_assistant(
        request_fastapi,
        agent_id,
        session,
        write=True,
    )
    return await _post_assistant_chat_message(
        session,
        assistant=assistant,
        body=body,
    )


@admin_router.post(
    "/chat/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_chat_message_as_assistant(
    body: AdminAssistantChatMessageCreate,
    session: Session = Depends(get_db_session),
) -> ChatMessageResponse:
    """Assistant runtime posting a message (admin auth)."""
    assistant = session.get(Assistant, body.assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found",
        )
    return await _post_assistant_chat_message(
        session,
        assistant=assistant,
        body=body,
    )


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


@router.post(
    "/chat/threads/{thread_id}/messages/{message_id}/reactions",
    response_model=ChatMessageResponse,
)
async def post_chat_message_reaction(
    request_fastapi: Request,
    thread_id: int,
    message_id: int,
    body: ChatReactionUpdate,
    session: Session = Depends(get_db_session),
) -> ChatMessageResponse:
    """Toggle the caller's emoji reaction on one message."""
    user_id = request_fastapi.state.user_id
    thread = _require_thread(session, thread_id)
    _require_human_thread_access(session, thread=thread, user_id=user_id)

    dao = ChatDAO(session)
    message = dao.get_message(message_id=message_id)
    if message is None or message.thread_id != thread.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    dao.set_message_reactions(
        message=message,
        reactions=apply_user_reaction(
            message.reactions if isinstance(message.reactions, list) else [],
            user_id=user_id,
            emoji=body.emoji,
        ),
    )
    session.commit()

    response = _message_response(message_payload(message, thread))
    await dispatch_chat_best_effort(
        {
            "kind": "reaction",
            "thread_id": thread.id,
            "thread_kind": thread.kind,
            "organization_id": thread.organization_id,
            "assistant_id": thread.assistant_id,
            "team_id": thread.team_id,
            "group_id": thread.group_id,
            "message": response.model_dump(),
            "fanout_assistant_ids": (
                [thread.assistant_id] if thread.kind == KIND_ASSISTANT_DM else []
            ),
            "reactor_user_id": user_id,
            "emoji": body.emoji,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Call utterances
# ---------------------------------------------------------------------------


def _store_utterances(
    session: Session,
    *,
    assistant: Assistant,
    body: CallUtterancesCreate,
) -> CallUtterancesPage:
    dao = ChatDAO(session)
    stored = [
        dao.add_utterance(
            call_id=body.call_id,
            reporter_assistant_id=assistant.agent_id,
            speaker_user_id=utterance.speaker_user_id,
            speaker_assistant_id=utterance.speaker_assistant_id,
            speaker_name=utterance.speaker_name,
            content=utterance.content,
            spoken_at=utterance.spoken_at,
            metadata=utterance.metadata,
        )
        for utterance in body.utterances
    ]
    session.commit()
    return CallUtterancesPage(
        call_id=body.call_id,
        utterances=[
            CallUtteranceResponse(
                id=utterance.id,
                call_id=utterance.call_id,
                reporter_assistant_id=utterance.reporter_assistant_id,
                speaker_user_id=utterance.speaker_user_id,
                speaker_assistant_id=utterance.speaker_assistant_id,
                speaker_name=utterance.speaker_name,
                content=utterance.content,
                spoken_at=utterance.spoken_at,
                metadata=utterance.meta or {},
            )
            for utterance in stored
        ],
    )


@router.post(
    "/assistant/{agent_id}/calls/utterances",
    response_model=CallUtterancesPage,
    status_code=status.HTTP_201_CREATED,
)
def post_call_utterances_as_owned_assistant(
    request_fastapi: Request,
    agent_id: int,
    body: CallUtterancesCreate,
    session: Session = Depends(get_db_session),
) -> CallUtterancesPage:
    """Assistant runtime appending call-transcript utterances (ownership auth)."""
    assistant = require_owned_assistant(
        request_fastapi,
        agent_id,
        session,
        write=True,
    )
    return _store_utterances(session, assistant=assistant, body=body)


@admin_router.post(
    "/calls/utterances",
    response_model=CallUtterancesPage,
    status_code=status.HTTP_201_CREATED,
)
def post_call_utterances_as_assistant(
    body: AdminCallUtterancesCreate,
    session: Session = Depends(get_db_session),
) -> CallUtterancesPage:
    """Assistant runtime appending call-transcript utterances (admin auth)."""
    assistant = session.get(Assistant, body.assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found",
        )
    return _store_utterances(session, assistant=assistant, body=body)


@router.get("/calls", response_model=CallsPage)
def list_calls(
    request_fastapi: Request,
    assistant_id: int = Query(...),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> CallsPage:
    """Call summaries transcribed by one assistant (most recent last)."""
    require_owned_assistant(request_fastapi, assistant_id, session)
    rows = ChatDAO(session).list_calls(
        reporter_assistant_id=assistant_id,
        limit=limit,
    )
    return CallsPage(
        calls=[
            CallSummaryResponse(
                call_id=row.call_id,
                started_at=row.started_at,
                ended_at=row.ended_at,
                utterance_count=row.utterance_count,
            )
            for row in rows
        ],
    )


@router.get("/calls/{call_id}/utterances", response_model=CallUtterancesPage)
def get_call_utterances(
    request_fastapi: Request,
    call_id: str,
    assistant_id: int = Query(...),
    limit: int = Query(1000, ge=1, le=5000),
    before_id: int | None = Query(None, ge=0),
    session: Session = Depends(get_db_session),
) -> CallUtterancesPage:
    """One assistant's transcript of a call (oldest first)."""
    require_owned_assistant(request_fastapi, assistant_id, session)
    utterances = ChatDAO(session).list_utterances(
        call_id=call_id,
        reporter_assistant_id=assistant_id,
        limit=limit,
        before_id=before_id,
    )
    return CallUtterancesPage(
        call_id=call_id,
        utterances=[
            CallUtteranceResponse(
                id=utterance.id,
                call_id=utterance.call_id,
                reporter_assistant_id=utterance.reporter_assistant_id,
                speaker_user_id=utterance.speaker_user_id,
                speaker_assistant_id=utterance.speaker_assistant_id,
                speaker_name=utterance.speaker_name,
                content=utterance.content,
                spoken_at=utterance.spoken_at,
                metadata=utterance.meta or {},
            )
            for utterance in utterances
        ],
    )
