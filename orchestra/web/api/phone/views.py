"""Admin endpoints for shared phone routing."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import CommunicationCallSession

admin_router = APIRouter()


class ResolveResponse(BaseModel):
    assistant_id: Optional[int] = None
    role: Optional[str] = None
    action: Optional[str] = None


class CallSessionUpsertRequest(BaseModel):
    provider: str = "twilio"
    provider_call_sid: str
    channel: str
    assistant_id: int
    from_number: str
    to_number: str
    pool_number: Optional[str] = None
    conference_name: str
    livekit_room: str
    status: str = "created"
    metadata: Optional[dict] = None


class CallSessionUpdateRequest(BaseModel):
    provider: str = "twilio"
    provider_call_sid: str
    status: Optional[str] = None
    recording_url: Optional[str] = None
    metadata: Optional[dict] = None


class CallSessionResponse(BaseModel):
    id: int
    provider: str
    provider_call_sid: str
    channel: str
    assistant_id: int
    from_number: str
    to_number: str
    pool_number: Optional[str]
    conference_name: str
    livekit_room: str
    status: str
    recording_url: Optional[str]
    metadata: Optional[dict]


@admin_router.get("/phone/resolve")
def resolve_inbound(
    pool_number: str = Query(..., description="The To number (pool number, E.164)."),
    sender: str = Query(..., description="The From number (sender, E.164)."),
    session: Session = Depends(get_db_session),
) -> ResolveResponse:
    """Resolve an inbound SMS or PSTN call to a Coordinator assistant."""
    result = SharedPoolDAO(session, platform="phone").resolve_inbound(
        pool_number,
        sender,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assistant found for this sender on this pool number.",
        )
    if "action" in result:
        return ResolveResponse(action=result["action"])
    return ResolveResponse(
        assistant_id=result["assistant_id"],
        role=result["role"],
    )


@admin_router.post("/phone/call-session")
def upsert_call_session(
    body: CallSessionUpsertRequest,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Create or refresh routing state for one provider PSTN call."""
    call_session = _get_call_session(
        session,
        provider=body.provider,
        provider_call_sid=body.provider_call_sid,
    )
    if call_session is None:
        call_session = CommunicationCallSession(
            provider=body.provider,
            provider_call_sid=body.provider_call_sid,
            channel=body.channel,
            assistant_id=body.assistant_id,
            from_number=body.from_number,
            to_number=body.to_number,
            pool_number=body.pool_number,
            conference_name=body.conference_name,
            livekit_room=body.livekit_room,
            status=body.status,
            metadata_=body.metadata,
        )
        session.add(call_session)
    else:
        call_session.channel = body.channel
        call_session.assistant_id = body.assistant_id
        call_session.from_number = body.from_number
        call_session.to_number = body.to_number
        call_session.pool_number = body.pool_number
        call_session.conference_name = body.conference_name
        call_session.livekit_room = body.livekit_room
        call_session.status = body.status
        call_session.metadata_ = body.metadata

    session.commit()
    session.refresh(call_session)
    return _call_session_response(call_session)


@admin_router.get("/phone/call-session/{provider_call_sid}")
def get_call_session(
    provider_call_sid: str,
    provider: str = Query("twilio"),
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Return the original routing state for one provider PSTN call."""
    return _call_session_response(
        _get_call_session_or_404(
            session,
            provider=provider,
            provider_call_sid=provider_call_sid,
        ),
    )


@admin_router.patch("/phone/call-session")
def update_call_session(
    body: CallSessionUpdateRequest,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Update provider status or recording metadata for a PSTN call."""
    call_session = _get_call_session_or_404(
        session,
        provider=body.provider,
        provider_call_sid=body.provider_call_sid,
    )
    if body.status is not None:
        call_session.status = body.status
    if body.recording_url is not None:
        call_session.recording_url = body.recording_url
    if body.metadata is not None:
        merged = dict(call_session.metadata_ or {})
        merged.update(body.metadata)
        call_session.metadata_ = merged
    session.commit()
    session.refresh(call_session)
    return _call_session_response(call_session)


def _get_call_session(
    session: Session,
    *,
    provider: str,
    provider_call_sid: str,
) -> CommunicationCallSession | None:
    return (
        session.query(CommunicationCallSession)
        .filter(
            CommunicationCallSession.provider == provider,
            CommunicationCallSession.provider_call_sid == provider_call_sid,
        )
        .first()
    )


def _get_call_session_or_404(
    session: Session,
    *,
    provider: str,
    provider_call_sid: str,
) -> CommunicationCallSession:
    call_session = _get_call_session(
        session,
        provider=provider,
        provider_call_sid=provider_call_sid,
    )
    if call_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Call session not found.",
        )
    return call_session


def _call_session_response(
    call_session: CommunicationCallSession,
) -> CallSessionResponse:
    return CallSessionResponse(
        id=call_session.id,
        provider=call_session.provider,
        provider_call_sid=call_session.provider_call_sid,
        channel=call_session.channel,
        assistant_id=call_session.assistant_id,
        from_number=call_session.from_number,
        to_number=call_session.to_number,
        pool_number=call_session.pool_number,
        conference_name=call_session.conference_name,
        livekit_room=call_session.livekit_room,
        status=call_session.status,
        recording_url=call_session.recording_url,
        metadata=call_session.metadata_,
    )
