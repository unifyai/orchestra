"""Unified call sessions: create/answer/join/decline/leave/end for every scope.

One signaling surface for human DM, team, group, and 1:1 assistant calls.
Sessions bind to the scope's ``chat_thread``; humans are participants;
assistants live in ``assistant_ids`` and Orchestra dispatches them into the
LiveKit room via the hosted adapters (``POST /unify/meet``) — the Console
never talks to adapters directly.

Ring/lifecycle frames are published through the comms control plane as
``kind="call"`` events; adapters route them to the per-org topic (org scopes)
or the assistant topic (``assistant_dm``).
"""

import datetime
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from orchestra.db.dao.chat_dao import ChatDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Assistant,
    CallParticipant,
    CallSession,
    ChatGroup,
    Organization,
    Team,
    User,
)
from orchestra.services.call_contacts import ensure_call_contacts
from orchestra.services.chat_group_service import (
    get_active_group,
    is_assistant_group_member,
    is_human_group_member,
)
from orchestra.services.chat_service import assistant_display_name
from orchestra.web.api.calls.schema import (
    AssistantCallCreate,
    CallAddAssistantRequest,
    CallCreate,
    CallCreateResponse,
    CallParticipantResponse,
    CallRosterMember,
    CallsActiveResponse,
    CallSessionResponse,
    OwnedAssistantCallCreate,
)
from orchestra.web.api.utils.assistant_infra import (
    ADAPTERS_URL,
    ADMIN_KEY,
    LOCAL_ADAPTERS_URL,
    dispatch_chat_best_effort,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant
from orchestra.web.api.utils.http_client import get_async_client

router = APIRouter()
admin_router = APIRouter()
logger = logging.getLogger(__name__)

CallAction = Literal[
    "incoming",
    "answered",
    "ended",
    "declined",
    "participant_joined",
    "participant_left",
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
            detail="You are not a member of this organization",
        )


def _require_call_session(session: Session, call_id: str) -> CallSession:
    call_session = session.scalar(
        select(CallSession).where(CallSession.id == call_id),
    )
    if call_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Call session not found",
        )
    # Ensure participants are loaded for response/event payloads.
    _ = list(call_session.participants)
    return call_session


def _participant_for_user(
    call_session: CallSession,
    *,
    user_id: str,
) -> CallParticipant | None:
    for participant in call_session.participants or []:
        if participant.user_id == user_id:
            return participant
    return None


def _require_call_participant(
    call_session: CallSession,
    *,
    user_id: str,
) -> CallParticipant:
    participant = _participant_for_user(call_session, user_id=user_id)
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only call participants can manage this call",
        )
    return participant


def _joined_human_count(call_session: CallSession) -> int:
    return sum(1 for p in call_session.participants or [] if p.status == "joined")


def _finalize_room_name(session: Session, call_session: CallSession) -> None:
    session.flush()
    call_session.livekit_room = f"unity_call_{call_session.id}"


def _display_roster(
    session: Session,
    call_session: CallSession,
) -> list[CallRosterMember]:
    """Display names/emails for everyone on the call.

    Purely informational (no Contacts side effects) — the per-assistant
    contact roster used for Meet dispatch is built by ``ensure_call_contacts``.
    """
    roster: list[CallRosterMember] = []
    user_ids = [p.user_id for p in (call_session.participants or [])]
    if user_ids:
        users = {
            u.id: u
            for u in session.scalars(select(User).where(User.id.in_(user_ids))).all()
        }
        for user_id in user_ids:
            user = users.get(user_id)
            if user is None:
                continue
            display = " ".join(
                part for part in [user.name or "", user.last_name or ""] if part
            ).strip() or (user.email or user.id)
            roster.append(
                CallRosterMember(
                    kind="human",
                    user_id=user.id,
                    display_name=display,
                    email=user.email,
                ),
            )
    assistant_ids = [int(a) for a in (call_session.assistant_ids or [])]
    if assistant_ids:
        assistants = {
            a.agent_id: a
            for a in session.scalars(
                select(Assistant).where(Assistant.agent_id.in_(assistant_ids)),
            ).all()
        }
        for assistant_id in assistant_ids:
            assistant = assistants.get(assistant_id)
            roster.append(
                CallRosterMember(
                    kind="assistant",
                    assistant_id=assistant_id,
                    display_name=(
                        assistant_display_name(assistant)
                        if assistant is not None
                        else f"Assistant {assistant_id}"
                    ),
                ),
            )
    return roster


def _call_event(call_session: CallSession) -> dict[str, Any]:
    participants = list(call_session.participants or [])
    user_ids = [p.user_id for p in participants]
    callee_user_id = None
    if call_session.scope == "dm":
        for participant in participants:
            if participant.user_id != call_session.created_by_user_id:
                callee_user_id = participant.user_id
                break
    session = object_session(call_session)
    roster = (
        [m.model_dump() for m in _display_roster(session, call_session)]
        if session is not None
        else []
    )
    return {
        "call_id": call_session.id,
        "room_name": call_session.livekit_room,
        "status": call_session.status,
        "scope": call_session.scope,
        "organization_id": call_session.organization_id,
        "thread_id": call_session.thread_id,
        "created_by_user_id": call_session.created_by_user_id,
        "created_by_assistant_id": call_session.created_by_assistant_id,
        "caller_user_id": call_session.created_by_user_id,
        "callee_user_id": callee_user_id,
        "team_id": call_session.team_id,
        "group_id": call_session.group_id,
        "user_ids": user_ids,
        "assistant_ids": [int(a) for a in (call_session.assistant_ids or [])],
        "participants": [
            {
                "user_id": participant.user_id,
                "role": participant.role,
                "status": participant.status,
            }
            for participant in participants
        ],
        "roster": roster,
    }


def _call_response(call_session: CallSession) -> CallSessionResponse:
    event = _call_event(call_session)
    return CallSessionResponse(
        call_id=event["call_id"],
        room_name=event["room_name"],
        status=event["status"],
        scope=event["scope"],
        organization_id=event["organization_id"],
        thread_id=event["thread_id"],
        created_by_user_id=event["created_by_user_id"],
        created_by_assistant_id=event["created_by_assistant_id"],
        caller_user_id=event["caller_user_id"],
        callee_user_id=event["callee_user_id"],
        team_id=event["team_id"],
        group_id=event["group_id"],
        user_ids=event["user_ids"],
        participants=[
            CallParticipantResponse(**participant)
            for participant in event["participants"]
        ],
        assistant_ids=event["assistant_ids"],
        roster=[CallRosterMember(**member) for member in event["roster"]],
    )


async def _dispatch_call_frame(
    *,
    action: CallAction,
    call_session: CallSession,
) -> None:
    await dispatch_chat_best_effort(
        {
            "kind": "call",
            "action": action,
            "organization_id": call_session.organization_id,
            "call": _call_event(call_session),
        },
    )


def _roster_payload(members: list[CallRosterMember]) -> list[dict[str, Any]]:
    return [
        {
            "kind": member.kind,
            "user_id": member.user_id,
            "assistant_id": member.assistant_id,
            "display_name": member.display_name,
            "contact_id": member.contact_id,
            "email": member.email,
        }
        for member in members
    ]


async def _post_meet_dispatch(
    call_session: CallSession,
    *,
    assistant_id: int,
    roster: list[CallRosterMember],
) -> None:
    """POST one assistant's Meet dispatch (session + roster) to adapters."""
    adapters_url = (LOCAL_ADAPTERS_URL or ADAPTERS_URL or "").rstrip("/")
    if not adapters_url or not ADMIN_KEY:
        return
    payload: dict[str, Any] = {
        "assistant_id": str(assistant_id),
        "room_name": call_session.livekit_room,
        "call_session_id": call_session.id,
        "participants": _roster_payload(roster),
    }
    if call_session.opening_config:
        payload["opening_config"] = call_session.opening_config
    try:
        await get_async_client().post(
            f"{adapters_url}/unify/meet",
            headers={
                "Authorization": f"Bearer {ADMIN_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except Exception:
        logger.exception(
            "Failed to dispatch assistant %s into call %s",
            assistant_id,
            call_session.id,
        )


async def _dispatch_assistants_to_meet(
    session: Session,
    *,
    call_session: CallSession,
    assistant_ids: list[int],
) -> None:
    """Ensure Contacts and dispatch/refresh each assistant into the room."""
    for assistant_id in assistant_ids:
        roster = ensure_call_contacts(
            session,
            call_session=call_session,
            for_assistant_id=assistant_id,
        )
        session.commit()
        await _post_meet_dispatch(
            call_session,
            assistant_id=assistant_id,
            roster=roster,
        )


def _add_participants(
    session: Session,
    *,
    call_session: CallSession,
    host_user_id: str | None,
    invited_user_ids: list[str],
    now: datetime.datetime,
) -> None:
    if host_user_id is not None:
        session.add(
            CallParticipant(
                call_id=call_session.id,
                user_id=host_user_id,
                role="host",
                status="joined",
                joined_at=now,
            ),
        )
    for user_id in invited_user_ids:
        session.add(
            CallParticipant(
                call_id=call_session.id,
                user_id=user_id,
                role="member",
                status="invited",
            ),
        )


def _assistant_ring_session(
    session: Session,
    *,
    assistant: Assistant,
    target_user_id: str,
    opening_config: dict[str, Any] | None,
) -> CallSession:
    """Create a ringing ``assistant_dm`` session (assistant calls its human)."""
    target = session.get(User, target_user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    thread = ChatDAO(session).resolve_assistant_dm_thread(
        assistant_id=assistant.agent_id,
        user_id=target_user_id,
        organization_id=assistant.organization_id,
    )
    call_session = CallSession(
        organization_id=assistant.organization_id,
        scope="assistant_dm",
        thread_id=thread.id,
        created_by_user_id=target_user_id,
        created_by_assistant_id=assistant.agent_id,
        livekit_room="pending",
        status="ringing",
        assistant_ids=[assistant.agent_id],
        opening_config=opening_config,
    )
    session.add(call_session)
    _finalize_room_name(session, call_session)
    _add_participants(
        session,
        call_session=call_session,
        host_user_id=None,
        invited_user_ids=[target_user_id],
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    session.commit()
    return _require_call_session(session, call_session.id)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post(
    "/calls",
    response_model=CallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_call(
    request_fastapi: Request,
    body: CallCreate,
    session: Session = Depends(get_db_session),
) -> CallCreateResponse:
    """Start a call against one chat scope the caller belongs to."""
    user_id = request_fastapi.state.user_id
    dao = ChatDAO(session)
    now = datetime.datetime.now(datetime.timezone.utc)

    if body.kind == "dm":
        if body.organization_id is None or not body.peer_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dm calls require organization_id and peer_user_id",
            )
        if body.peer_user_id == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot call yourself",
            )
        org = _require_org(session, body.organization_id)
        _require_org_member(session, org=org, user_id=user_id)
        _require_org_member(session, org=org, user_id=body.peer_user_id)
        thread = dao.resolve_dm_thread(
            organization_id=body.organization_id,
            user_id_1=user_id,
            user_id_2=body.peer_user_id,
        )
        call_session = CallSession(
            organization_id=body.organization_id,
            scope="dm",
            thread_id=thread.id,
            created_by_user_id=user_id,
            livekit_room="pending",
            status="ringing",
            assistant_ids=[],
        )
        session.add(call_session)
        _finalize_room_name(session, call_session)
        _add_participants(
            session,
            call_session=call_session,
            host_user_id=user_id,
            invited_user_ids=[body.peer_user_id],
            now=now,
        )

    elif body.kind == "team":
        if body.team_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="team calls require team_id",
            )
        team = session.get(Team, body.team_id)
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found",
            )
        org = _require_org(session, team.organization_id)
        _require_org_member(session, org=org, user_id=user_id)
        team_dao = TeamDAO(session)
        if not team_dao.is_team_member(team.id, user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of this team to start a call",
            )
        member_ids = team_dao.get_team_members(team.id)
        thread = dao.resolve_team_thread(
            team_id=team.id,
            organization_id=team.organization_id,
        )
        call_session = CallSession(
            organization_id=team.organization_id,
            scope="team",
            thread_id=thread.id,
            team_id=team.id,
            created_by_user_id=user_id,
            livekit_room="pending",
            status="ringing",
            # A room call is answered the moment it exists: its host is a
            # participant, not a caller waiting for someone to pick up. Left
            # unset, a call nobody else joined reported as missed with zero
            # duration however long the host and the assistants talked — and in
            # a one-human org that is every room call. ``status`` deliberately
            # stays "ringing" so invitees still ring and the stale-call sweep
            # keeps using the ring window.
            answered_at=now,
            assistant_ids=[],
        )
        session.add(call_session)
        _finalize_room_name(session, call_session)
        _add_participants(
            session,
            call_session=call_session,
            host_user_id=user_id,
            invited_user_ids=[m for m in member_ids if m != user_id],
            now=now,
        )

    elif body.kind == "group":
        if body.group_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="group calls require group_id",
            )
        group_row = session.scalar(
            select(ChatGroup).where(
                ChatGroup.id == body.group_id,
                ChatGroup.status == "active",
            ),
        )
        if group_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat group not found",
            )
        group = get_active_group(
            session,
            organization_id=group_row.organization_id,
            group_id=body.group_id,
        )
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat group not found",
            )
        org = _require_org(session, group.organization_id)
        _require_org_member(session, org=org, user_id=user_id)
        if not is_human_group_member(
            session,
            group_id=group.id,
            user_id=user_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of this group to start a call",
            )
        member_ids = [m.user_id for m in (group.members or []) if m.user_id]
        thread = dao.resolve_group_thread(
            group_id=group.id,
            organization_id=group.organization_id,
        )
        call_session = CallSession(
            organization_id=group.organization_id,
            scope="group",
            thread_id=thread.id,
            group_id=group.id,
            created_by_user_id=user_id,
            livekit_room="pending",
            status="ringing",
            # Answered on creation for the same reason as a team call — see the
            # team branch above.
            answered_at=now,
            assistant_ids=[],
        )
        session.add(call_session)
        _finalize_room_name(session, call_session)
        _add_participants(
            session,
            call_session=call_session,
            host_user_id=user_id,
            invited_user_ids=[m for m in member_ids if m != user_id],
            now=now,
        )

    else:  # assistant_dm
        if body.assistant_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assistant_dm calls require assistant_id",
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
        # No human to ring: the caller joins immediately and the assistant is
        # dispatched into the room, so the session starts active.
        call_session = CallSession(
            organization_id=assistant.organization_id,
            scope="assistant_dm",
            thread_id=thread.id,
            created_by_user_id=user_id,
            livekit_room="pending",
            status="active",
            answered_at=now,
            assistant_ids=[assistant.agent_id],
            opening_config=body.opening_config,
        )
        session.add(call_session)
        _finalize_room_name(session, call_session)
        _add_participants(
            session,
            call_session=call_session,
            host_user_id=user_id,
            invited_user_ids=[],
            now=now,
        )

    session.commit()
    call_session = _require_call_session(session, call_session.id)

    if call_session.scope == "assistant_dm":
        await _dispatch_assistants_to_meet(
            session,
            call_session=call_session,
            assistant_ids=[int(a) for a in call_session.assistant_ids],
        )
        await _dispatch_call_frame(action="answered", call_session=call_session)
    else:
        await _dispatch_call_frame(action="incoming", call_session=call_session)
    return CallCreateResponse(**_call_response(call_session).model_dump())


@router.post(
    "/assistant/{agent_id}/calls",
    response_model=CallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_call_as_owned_assistant(
    request_fastapi: Request,
    agent_id: int,
    body: OwnedAssistantCallCreate,
    session: Session = Depends(get_db_session),
) -> CallCreateResponse:
    """Assistant runtime ringing its human (ownership auth)."""
    assistant = require_owned_assistant(
        request_fastapi,
        agent_id,
        session,
        write=True,
    )
    call_session = _assistant_ring_session(
        session,
        assistant=assistant,
        target_user_id=body.user_id or assistant.user_id,
        opening_config=body.opening_config,
    )
    await _dispatch_call_frame(action="incoming", call_session=call_session)
    return CallCreateResponse(**_call_response(call_session).model_dump())


@admin_router.post(
    "/calls",
    response_model=CallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_assistant_call_as_assistant(
    body: AssistantCallCreate,
    session: Session = Depends(get_db_session),
) -> CallCreateResponse:
    """Assistant runtime ringing its human (admin auth)."""
    assistant = session.get(Assistant, body.assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found",
        )
    call_session = _assistant_ring_session(
        session,
        assistant=assistant,
        target_user_id=body.user_id or assistant.user_id,
        opening_config=body.opening_config,
    )
    await _dispatch_call_frame(action="incoming", call_session=call_session)
    return CallCreateResponse(**_call_response(call_session).model_dump())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


# A ring that was never answered cannot still be live after this long; the
# creating surface (Console engine or assistant runtime) ends unanswered
# rings itself, so anything older is an orphan from a crashed caller.
_RING_EXPIRY = datetime.timedelta(minutes=10)

# Active sessions end when the last joined human leaves — but a crashed
# browser never posts /leave, leaving the session active forever and
# re-offering a "rejoin" banner on every reload. No legitimate call runs
# this long.
_ACTIVE_EXPIRY = datetime.timedelta(hours=24)


@router.get("/calls/active", response_model=CallsActiveResponse)
def list_active_calls(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> CallsActiveResponse:
    """Live (ringing/active) calls that include the caller as a participant.

    Powers the Console rejoin banner after a page reload: the app-level call
    engine re-attaches to any call the user was on. Orphaned rings (the
    caller crashed before answering/cancelling) are lazily ended here so
    they can never surface as a permanently resumable ghost call.
    """
    user_id = request_fastapi.state.user_id
    call_sessions = (
        session.scalars(
            select(CallSession)
            .join(
                CallParticipant,
                CallParticipant.call_id == CallSession.id,
            )
            .where(
                CallSession.status.in_(["ringing", "active"]),
                CallParticipant.user_id == user_id,
            )
            .order_by(CallSession.created_at.desc()),
        )
        .unique()
        .all()
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = [
        call_session
        for call_session in call_sessions
        if (
            call_session.status == "ringing"
            and call_session.created_at < now - _RING_EXPIRY
        )
        or (
            call_session.status == "active"
            and (call_session.answered_at or call_session.created_at)
            < now - _ACTIVE_EXPIRY
        )
    ]
    if stale:
        for call_session in stale:
            call_session.status = "ended"
            call_session.ended_at = now
        session.commit()
        call_sessions = [c for c in call_sessions if c not in stale]
    return CallsActiveResponse(
        calls=[_call_response(call_session) for call_session in call_sessions],
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@router.post("/calls/{call_id}/answer", response_model=CallSessionResponse)
async def answer_call(
    request_fastapi: Request,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Mark an invited participant as joined and activate the call."""
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    participant = _require_call_participant(call_session, user_id=user_id)
    if participant.status == "joined" and call_session.status == "active":
        return _call_response(call_session)
    if call_session.status == "ended":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot answer a call that has ended",
        )
    if participant.status not in {"invited", "joined"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot answer with participant status '{participant.status}'",
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "joined"
    participant.joined_at = participant.joined_at or now
    participant.left_at = None
    if call_session.status == "ringing":
        call_session.status = "active"
        # Never re-stamped: a room call is already answered at creation, and
        # overwriting here would restart its clock at the first join, hiding
        # everything the host and the assistants did before that.
        call_session.answered_at = call_session.answered_at or now
    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(action="answered", call_session=call_session)
    if call_session.assistant_ids:
        # Assistant rings dispatch here (never before the human answers);
        # already-running assistants get a roster refresh through the same
        # idempotent dispatch.
        await _dispatch_assistants_to_meet(
            session,
            call_session=call_session,
            assistant_ids=[int(a) for a in call_session.assistant_ids],
        )
    return _call_response(call_session)


@router.post("/calls/{call_id}/join", response_model=CallSessionResponse)
async def join_call(
    request_fastapi: Request,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Late-join a live call as an invitee or team/group member."""
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    if call_session.status not in {"ringing", "active"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Call is not joinable",
        )

    participant = _participant_for_user(call_session, user_id=user_id)
    if participant is None:
        if call_session.scope == "team" and call_session.team_id is not None:
            if not TeamDAO(session).is_team_member(call_session.team_id, user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only team members can join this call",
                )
        elif call_session.scope == "group" and call_session.group_id is not None:
            if not is_human_group_member(
                session,
                group_id=call_session.group_id,
                user_id=user_id,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only group members can join this call",
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only call invitees can join this call",
            )
        participant = CallParticipant(
            call_id=call_session.id,
            user_id=user_id,
            role="member",
            status="invited",
        )
        session.add(participant)
        session.flush()

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "joined"
    participant.joined_at = participant.joined_at or now
    participant.left_at = None
    if call_session.status == "ringing":
        call_session.status = "active"
        # Never re-stamped: a room call is already answered at creation, and
        # overwriting here would restart its clock at the first join, hiding
        # everything the host and the assistants did before that.
        call_session.answered_at = call_session.answered_at or now
    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(
        action="participant_joined",
        call_session=call_session,
    )
    if call_session.assistant_ids:
        await _dispatch_assistants_to_meet(
            session,
            call_session=call_session,
            assistant_ids=[int(a) for a in call_session.assistant_ids],
        )
    return _call_response(call_session)


@router.post("/calls/{call_id}/decline", response_model=CallSessionResponse)
async def decline_call(
    request_fastapi: Request,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Decline a ringing invite. DM/assistant declines end the call."""
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    participant = _require_call_participant(call_session, user_id=user_id)
    if participant.role == "host":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Host cannot decline; end the call instead",
        )
    if participant.status == "declined" and call_session.scope in {"team", "group"}:
        return _call_response(call_session)
    if call_session.status == "ended":
        return _call_response(call_session)
    if participant.status not in {"invited", "declined"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot decline with participant status '{participant.status}'",
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "declined"
    participant.left_at = now

    action: CallAction = "declined"
    if call_session.scope in {"dm", "assistant_dm"}:
        call_session.status = "ended"
        call_session.ended_at = now
        if call_session.scope == "dm":
            action = "ended"
    else:
        # End the call only when every non-host invitee has declined and
        # nobody else has joined.
        pending = [
            p
            for p in call_session.participants
            if p.role != "host" and p.status == "invited"
        ]
        joined_others = [
            p
            for p in call_session.participants
            if p.role != "host" and p.status == "joined"
        ]
        if not pending and not joined_others:
            call_session.status = "ended"
            call_session.ended_at = now
            action = "ended"

    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(action=action, call_session=call_session)
    return _call_response(call_session)


@router.post("/calls/{call_id}/leave", response_model=CallSessionResponse)
async def leave_call(
    request_fastapi: Request,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Leave a live call without ending it for remaining humans."""
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    participant = _require_call_participant(call_session, user_id=user_id)
    if call_session.status == "ended":
        return _call_response(call_session)

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "left"
    participant.left_at = now
    session.flush()

    action: CallAction = "participant_left"
    if _joined_human_count(call_session) == 0:
        call_session.status = "ended"
        call_session.ended_at = now
        action = "ended"
    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(action=action, call_session=call_session)
    return _call_response(call_session)


@router.post("/calls/{call_id}/end", response_model=CallSessionResponse)
async def end_call(
    request_fastapi: Request,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """End a call for every participant."""
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    participant = _require_call_participant(call_session, user_id=user_id)
    is_host = participant.role == "host"
    is_last_human = (
        _joined_human_count(call_session) <= 1 and participant.status == "joined"
    )
    # assistant_dm calls belong to their human: they may always end them
    # (including cancelling an unanswered assistant ring).
    if (
        call_session.scope != "assistant_dm"
        and not is_host
        and not is_last_human
        and call_session.status != "ended"
    ):
        if participant.status != "joined":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the host can end this call for everyone",
            )
    if call_session.status == "ended":
        return _call_response(call_session)

    now = datetime.datetime.now(datetime.timezone.utc)
    call_session.status = "ended"
    call_session.ended_at = now
    for part in call_session.participants:
        if part.status == "joined":
            part.status = "left"
            part.left_at = now
    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(action="ended", call_session=call_session)
    return _call_response(call_session)


@router.post(
    "/assistant/{agent_id}/calls/{call_id}/end",
    response_model=CallSessionResponse,
)
async def end_call_as_owned_assistant(
    request_fastapi: Request,
    agent_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Assistant runtime ending its own call (e.g. an unanswered ring)."""
    assistant = require_owned_assistant(
        request_fastapi,
        agent_id,
        session,
        write=True,
    )
    call_session = _require_call_session(session, call_id)
    if assistant.agent_id not in [int(a) for a in (call_session.assistant_ids or [])]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assistant is not on this call",
        )
    if call_session.status == "ended":
        return _call_response(call_session)

    now = datetime.datetime.now(datetime.timezone.utc)
    call_session.status = "ended"
    call_session.ended_at = now
    for part in call_session.participants:
        if part.status == "joined":
            part.status = "left"
            part.left_at = now
    session.commit()
    call_session = _require_call_session(session, call_id)

    await _dispatch_call_frame(action="ended", call_session=call_session)
    return _call_response(call_session)


# ---------------------------------------------------------------------------
# Assistants on a call
# ---------------------------------------------------------------------------


@router.post("/calls/{call_id}/assistants", response_model=CallSessionResponse)
async def add_assistant_to_call(
    request_fastapi: Request,
    call_id: str,
    body: CallAddAssistantRequest,
    session: Session = Depends(get_db_session),
) -> CallSessionResponse:
    """Add an assistant to a live call and dispatch it into the room.

    Idempotent for assistants already on the call — the redispatch pushes a
    fresh roster, which is also how the Console recovers a dropped agent.
    """
    user_id = request_fastapi.state.user_id
    call_session = _require_call_session(session, call_id)
    _require_call_participant(call_session, user_id=user_id)
    if call_session.status not in {"ringing", "active"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot add an assistant to a finished call",
        )
    assistant = session.get(Assistant, body.assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found",
        )
    # Roster filtering already hides private coordinators from the picker,
    # but the invariant is enforced here too: a single-player twin never
    # joins a call with anyone but its owner (assistant_dm).
    if assistant.is_private_coordinator and call_session.scope != "assistant_dm":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Private coordinators cannot join multi-party calls",
        )

    if call_session.scope == "team" and call_session.team_id is not None:
        membership = TeamDAO(session).get_assistant_membership(
            team_id=call_session.team_id,
            assistant_id=body.assistant_id,
        )
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Assistant must be a member of this team",
            )
    elif call_session.scope == "group" and call_session.group_id is not None:
        if not is_assistant_group_member(
            session,
            group_id=call_session.group_id,
            assistant_id=body.assistant_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Assistant must be a member of this group",
            )
    else:
        # DM and assistant_dm scopes: the assistant must belong to a human on
        # the call (or share the call's organization).
        participant_user_ids = {p.user_id for p in call_session.participants or []}
        if assistant.user_id not in participant_user_ids and (
            call_session.organization_id is None
            or assistant.organization_id != call_session.organization_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Assistant is not available to this call",
            )

    was_added = False
    assistant_ids = [int(a) for a in (call_session.assistant_ids or [])]
    if body.assistant_id not in assistant_ids:
        assistant_ids.append(body.assistant_id)
        call_session.assistant_ids = assistant_ids
        session.commit()
        call_session = _require_call_session(session, call_id)
        was_added = True

    # Dispatch the (new or recovering) assistant with a contact-bearing
    # roster, then refresh peers so their rosters include it.
    roster = ensure_call_contacts(
        session,
        call_session=call_session,
        for_assistant_id=body.assistant_id,
    )
    session.commit()
    await _post_meet_dispatch(
        call_session,
        assistant_id=body.assistant_id,
        roster=roster,
    )
    if was_added:
        await _dispatch_call_frame(
            action="participant_joined",
            call_session=call_session,
        )
        peer_ids = [
            int(a)
            for a in (call_session.assistant_ids or [])
            if int(a) != body.assistant_id
        ]
        if peer_ids:
            await _dispatch_assistants_to_meet(
                session,
                call_session=call_session,
                assistant_ids=peer_ids,
            )
    response = _call_response(call_session)
    response.roster = roster
    return response
