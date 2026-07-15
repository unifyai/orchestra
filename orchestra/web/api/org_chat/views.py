"""Org roster, team group chat, and human DM endpoints.

The roster powers the Console top selector (humans + teams alongside the
assistant list). Team messages persist to the ``Teams/{team_id}/GroupChat``
log context and are delivered by the hosted communication layer (adapters
``/unify/org-chat``): a per-organization Pub/Sub topic for Console SSE plus
standard ``unify_message`` envelopes to every non-coordinator team assistant
(team chat is ordinary unify_message traffic, like a large email CC chain).
DMs persist to Postgres and only publish the Console frame — no assistant is
ever involved in a human-to-human DM.
"""

import datetime
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.dm_dao import DmDAO, normalized_pair
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dao.user_presence_dao import UserPresenceDAO, presence_is_online
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    DmThread,
    HumanCallSession,
    Organization,
    Team,
    User,
)
from orchestra.services.bucket_service import create_bucket_service
from orchestra.services.org_chat_service import (
    SENDER_KIND_ASSISTANT,
    SENDER_KIND_USER,
    assistant_email,
    build_team_dispatch_payload,
    list_team_messages,
    persist_team_message,
    search_team_messages,
)
from orchestra.web.api.org_chat.schema import (
    AssistantTeamMessageCreate,
    DmMessageCreate,
    DmMessageResponse,
    DmMessagesPage,
    HumanCallCreateResponse,
    HumanCallSessionResponse,
    OrgChatAttachment,
    OrgChatSearchPage,
    OrgChatSearchResult,
    OrgRosterResponse,
    RosterHuman,
    RosterTeam,
    TeamMessageCreate,
    TeamMessageResponse,
    TeamMessagesPage,
)
from orchestra.web.api.utils.assistant_infra import dispatch_org_chat_best_effort
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant
from orchestra.web.api.utils.gcp import parse_gcs_url

router = APIRouter()
admin_router = APIRouter()
logger = logging.getLogger(__name__)


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


def _enrich_attachments(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
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


def _attachments_for_storage(
    attachments: list[OrgChatAttachment],
) -> list[dict[str, Any]]:
    return [
        attachment.model_dump(
            exclude_none=True,
            exclude={"signed_url"},
        )
        for attachment in attachments
    ]


def _dm_message_response(message) -> DmMessageResponse:
    return DmMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        sender_user_id=message.sender_user_id,
        content=message.content,
        created_at=message.created_at,
        attachments=_enrich_attachments(message.attachments),
    )


def _team_message_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = dict(message)
    payload["mentions"] = payload.get("mentions") or []
    payload["attachments"] = _enrich_attachments(payload.get("attachments"))
    return payload


def _human_call_event(call_session: HumanCallSession) -> dict[str, Any]:
    return {
        "call_id": call_session.id,
        "room_name": call_session.livekit_room,
        "status": call_session.status,
        "caller_user_id": call_session.caller_user_id,
        "callee_user_id": call_session.callee_user_id,
        "organization_id": call_session.organization_id,
        "thread_id": call_session.thread_id,
        "user_ids": [
            call_session.caller_user_id,
            call_session.callee_user_id,
        ],
    }


def _human_call_response(
    call_session: HumanCallSession,
) -> HumanCallSessionResponse:
    return HumanCallSessionResponse(
        call_id=call_session.id,
        room_name=call_session.livekit_room,
        status=call_session.status,
        caller_user_id=call_session.caller_user_id,
        callee_user_id=call_session.callee_user_id,
    )


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


def _require_team(session: Session, *, organization_id: int, team_id: int) -> Team:
    team = TeamDAO(session).get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    return team


def _display_name(user: User) -> str:
    return " ".join(part for part in [user.name, user.last_name] if part) or user.email


@router.get(
    "/organizations/{organization_id}/roster",
    response_model=OrgRosterResponse,
)
def get_org_roster(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> OrgRosterResponse:
    """Selector payload: org humans (with presence) and teams (with members).

    Coordinators are deliberately excluded from every team's
    ``assistant_member_ids``: their memberships exist for shared-memory
    access, not for the roster or group chat.
    """
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)

    member_dao = OrganizationMemberDAO(session)
    members = member_dao.get_members_with_details(organization_id)

    member_ids = [member["user_id"] for member in members]
    owner_ids = [org.owner_id] if org.owner_id not in set(member_ids) else []
    presence_map = UserPresenceDAO(session).last_seen_map(member_ids + owner_ids)

    humans: list[RosterHuman] = []
    for member in members:
        last_seen = presence_map.get(member["user_id"])
        humans.append(
            RosterHuman(
                user_id=member["user_id"],
                name=member.get("name"),
                email=member.get("email"),
                image=member.get("image"),
                role_name=member.get("role_name"),
                bio=member.get("bio"),
                job_title=member.get("job_title"),
                phone_number=member.get("phone_number"),
                whatsapp_number=member.get("whatsapp_number"),
                timezone=member.get("timezone"),
                online=presence_is_online(last_seen),
                last_seen_at=last_seen,
            ),
        )
    if owner_ids:
        owner = session.get(User, org.owner_id)
        if owner is not None:
            last_seen = presence_map.get(owner.id)
            humans.append(
                RosterHuman(
                    user_id=owner.id,
                    name=_display_name(owner),
                    email=owner.email,
                    image=owner.image,
                    role_name="Owner",
                    bio=owner.bio,
                    job_title=owner.job_title,
                    phone_number=owner.phone_number,
                    whatsapp_number=owner.whatsapp_number,
                    timezone=owner.timezone,
                    online=presence_is_online(last_seen),
                    last_seen_at=last_seen,
                ),
            )

    team_dao = TeamDAO(session)
    teams: list[RosterTeam] = []
    for team in team_dao.list_organization_teams(organization_id):
        assistant_member_ids = [
            assistant.agent_id
            for _, assistant in team_dao.list_assistant_members(team.id)
            if not assistant.is_coordinator
        ]
        teams.append(
            RosterTeam(
                team_id=team.id,
                name=team.name,
                description=team.description,
                is_org_wide_sharing=team.is_org_wide_sharing,
                created_at=team.created_at,
                member_user_ids=team_dao.get_team_members(team.id),
                assistant_member_ids=assistant_member_ids,
                image=team.image,
            ),
        )

    return OrgRosterResponse(
        organization_id=organization_id,
        humans=humans,
        teams=teams,
    )


@router.get(
    "/organizations/{organization_id}/teams/{team_id}/messages",
    response_model=TeamMessagesPage,
)
def get_team_messages(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    limit: int = Query(100, ge=1, le=500),
    before_message_id: int | None = Query(None, ge=0),
    session: Session = Depends(get_db_session),
) -> TeamMessagesPage:
    """Team group-chat history (most recent last)."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    team = _require_team(session, organization_id=organization_id, team_id=team_id)
    if not TeamDAO(session).is_team_member(team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this team to view its chat",
        )

    messages = list_team_messages(
        session,
        team=team,
        limit=limit,
        before_message_id=before_message_id,
    )
    return TeamMessagesPage(
        messages=[
            TeamMessageResponse(**_team_message_payload(message))
            for message in messages
        ],
    )


@router.post(
    "/organizations/{organization_id}/teams/{team_id}/messages",
    response_model=TeamMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_team_message(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    body: TeamMessageCreate,
    session: Session = Depends(get_db_session),
) -> TeamMessageResponse:
    """Post a message to a team group chat as the authenticated human.

    Persists the message, then hands realtime delivery + assistant fan-out to
    the hosted communication layer (best-effort: a hosted hiccup does not
    fail the accepted message).
    """
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    team = _require_team(session, organization_id=organization_id, team_id=team_id)
    if not TeamDAO(session).is_team_member(team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this team to post in its chat",
        )

    sender = session.get(User, user_id)
    if sender is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    try:
        message = persist_team_message(
            session,
            team=team,
            sender_kind=SENDER_KIND_USER,
            sender_user_id=user_id,
            sender_assistant_id=None,
            sender_name=_display_name(sender),
            content=body.content,
            mentions=[mention.model_dump() for mention in body.mentions],
            attachments=_attachments_for_storage(body.attachments),
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    response_message = _team_message_payload(message)
    payload = build_team_dispatch_payload(
        session,
        team=team,
        message=response_message,
        sender_email=sender.email or "",
    )
    await dispatch_org_chat_best_effort(payload)
    return TeamMessageResponse(**response_message)


async def _post_team_message_from_assistant(
    session: Session,
    *,
    team_id: int,
    assistant_id: int,
    content: str,
    mentions: list,
    attachments: list[dict[str, Any]],
) -> TeamMessageResponse:
    """Persist and fan out one assistant-authored team group-chat message.

    The reply is persisted, published to the Console stream, and fanned out
    to every other non-coordinator team assistant (the author is excluded —
    it already knows what it said) so AI replies are part of every
    teammate's conversational context, exactly like a human message.
    """
    team = TeamDAO(session).get(team_id)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found",
        )
    team_dao = TeamDAO(session)
    membership = team_dao.get_assistant_membership(
        team_id=team_id,
        assistant_id=assistant_id,
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assistant is not a member of this team",
        )
    assistant = team_dao.get_assistant(assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found",
        )
    sender_name = (
        " ".join(part for part in [assistant.first_name, assistant.surname] if part)
        or f"Assistant {assistant.agent_id}"
    )

    try:
        message = persist_team_message(
            session,
            team=team,
            sender_kind=SENDER_KIND_ASSISTANT,
            sender_user_id=None,
            sender_assistant_id=assistant.agent_id,
            sender_name=sender_name,
            content=content,
            mentions=[mention.model_dump() for mention in mentions],
            attachments=attachments,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    response_message = _team_message_payload(message)
    payload = build_team_dispatch_payload(
        session,
        team=team,
        message=response_message,
        sender_email=assistant_email(session, assistant.agent_id),
        exclude_assistant_id=assistant.agent_id,
    )
    await dispatch_org_chat_best_effort(payload)
    return TeamMessageResponse(**response_message)


@admin_router.post(
    "/teams/{team_id}/messages",
    response_model=TeamMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_team_message_as_assistant(
    team_id: int,
    body: AssistantTeamMessageCreate,
    session: Session = Depends(get_db_session),
) -> TeamMessageResponse:
    """Assistant runtime posting a group-chat reply (admin auth)."""
    return await _post_team_message_from_assistant(
        session,
        team_id=team_id,
        assistant_id=body.assistant_id,
        content=body.content,
        mentions=body.mentions,
        attachments=_attachments_for_storage(body.attachments),
    )


@router.post(
    "/assistant/{agent_id}/teams/{team_id}/messages",
    response_model=TeamMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_team_message_as_owned_assistant(
    request_fastapi: Request,
    agent_id: int,
    team_id: int,
    body: TeamMessageCreate,
    session: Session = Depends(get_db_session),
) -> TeamMessageResponse:
    """Assistant runtime posting a group-chat reply (ownership-scoped auth).

    User-API-key equivalent of the admin route: the caller must own the
    assistant identified in the path; the assistant must still be a member
    of the target team.
    """
    require_owned_assistant(request_fastapi, agent_id, session, write=True)
    return await _post_team_message_from_assistant(
        session,
        team_id=team_id,
        assistant_id=agent_id,
        content=body.content,
        mentions=body.mentions,
        attachments=_attachments_for_storage(body.attachments),
    )


@router.get(
    "/organizations/{organization_id}/org-chat/search",
    response_model=OrgChatSearchPage,
)
def search_org_chat(
    request_fastapi: Request,
    organization_id: int,
    q: str = Query(..., min_length=1, max_length=200),
    scope: Literal["dm", "team"] = Query(...),
    scope_id: str = Query(..., alias="id", min_length=1),
    session: Session = Depends(get_db_session),
) -> OrgChatSearchPage:
    """Search one DM thread or one team GroupChat thread by message content."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    needle = q.strip()
    if not needle:
        return OrgChatSearchPage(results=[])

    if scope == "dm":
        other = _require_dm_counterpart(
            session,
            org=org,
            user_id=user_id,
            other_user_id=scope_id,
        )
        user_a_id, user_b_id = normalized_pair(user_id, other.id)
        thread = session.scalar(
            select(DmThread).where(
                DmThread.organization_id == organization_id,
                DmThread.user_a_id == user_a_id,
                DmThread.user_b_id == user_b_id,
            ),
        )
        if thread is None:
            return OrgChatSearchPage(results=[])
        messages = DmDAO(session).search_messages(
            thread_id=thread.id,
            q=needle,
        )
        sender_ids = {
            message.sender_user_id
            for message in messages
            if message.sender_user_id is not None
        }
        senders = (
            {
                sender.id: _display_name(sender)
                for sender in session.query(User).filter(User.id.in_(sender_ids)).all()
            }
            if sender_ids
            else {}
        )
        return OrgChatSearchPage(
            results=[
                OrgChatSearchResult(
                    id=str(message.id),
                    scope="dm",
                    content=message.content,
                    timestamp=message.created_at.isoformat(),
                    sender_name=senders.get(message.sender_user_id, "Unknown"),
                )
                for message in messages
            ],
        )

    try:
        team_id = int(scope_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Team search id must be an integer team id",
        ) from exc

    team = _require_team(session, organization_id=organization_id, team_id=team_id)
    if not TeamDAO(session).is_team_member(team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this team to search its chat",
        )
    matches = search_team_messages(
        session,
        team=team,
        q=needle,
    )
    return OrgChatSearchPage(
        results=[
            OrgChatSearchResult(
                id=str(match.get("message_id") or ""),
                scope="team",
                content=str(match.get("content") or ""),
                timestamp=match.get("timestamp") or None,
                sender_name=str(match.get("sender_name") or "Unknown"),
            )
            for match in matches
            if match.get("message_id") is not None
        ],
    )


def _require_dm_counterpart(
    session: Session,
    *,
    org: Organization,
    user_id: str,
    other_user_id: str,
) -> User:
    if other_user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot open a DM with yourself",
        )
    other = session.get(User, other_user_id)
    if other is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    _require_org_member(session, org=org, user_id=other_user_id)
    return other


@router.get(
    "/organizations/{organization_id}/dms/{other_user_id}/messages",
    response_model=DmMessagesPage,
)
def get_dm_messages(
    request_fastapi: Request,
    organization_id: int,
    other_user_id: str,
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(None, ge=0),
    q: str | None = Query(None, min_length=1, max_length=200),
    session: Session = Depends(get_db_session),
) -> DmMessagesPage:
    """DM history between the authenticated user and one org member."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    _require_dm_counterpart(
        session,
        org=org,
        user_id=user_id,
        other_user_id=other_user_id,
    )

    dm_dao = DmDAO(session)
    thread = dm_dao.get_or_create_thread(
        organization_id=organization_id,
        user_id_1=user_id,
        user_id_2=other_user_id,
    )
    session.commit()
    messages = dm_dao.list_messages(
        thread_id=thread.id,
        limit=limit,
        before_id=before_id,
        q=q,
    )
    return DmMessagesPage(
        thread_id=thread.id,
        organization_id=organization_id,
        user_ids=list(normalized_pair(user_id, other_user_id)),
        messages=[_dm_message_response(message) for message in messages],
    )


@router.post(
    "/organizations/{organization_id}/dms/{other_user_id}/messages",
    response_model=DmMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_dm_message(
    request_fastapi: Request,
    organization_id: int,
    other_user_id: str,
    body: DmMessageCreate,
    session: Session = Depends(get_db_session),
) -> DmMessageResponse:
    """Send a DM to another org member.

    Persists to Postgres and publishes one Console frame to the org topic;
    no assistant runtime is involved.
    """
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    _require_dm_counterpart(
        session,
        org=org,
        user_id=user_id,
        other_user_id=other_user_id,
    )

    sender = session.get(User, user_id)
    if sender is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    dm_dao = DmDAO(session)
    thread = dm_dao.get_or_create_thread(
        organization_id=organization_id,
        user_id_1=user_id,
        user_id_2=other_user_id,
    )
    message = dm_dao.add_message(
        thread=thread,
        sender_user_id=user_id,
        content=body.content,
        attachments=_attachments_for_storage(body.attachments),
    )
    session.commit()

    response_message = _dm_message_response(message)
    dispatch_message = {
        "id": message.id,
        "thread_id": thread.id,
        "organization_id": organization_id,
        "user_ids": list(normalized_pair(user_id, other_user_id)),
        "sender_user_id": user_id,
        "sender_name": _display_name(sender),
        "content": message.content,
        "attachments": response_message.model_dump()["attachments"],
        "timestamp": message.created_at.isoformat(),
    }

    await dispatch_org_chat_best_effort(
        {
            "kind": "dm",
            "organization_id": organization_id,
            "message": dispatch_message,
        },
    )

    return response_message


def _require_human_call_session(
    session: Session,
    *,
    organization_id: int,
    call_id: str,
) -> HumanCallSession:
    call_session = session.scalar(
        select(HumanCallSession).where(
            HumanCallSession.id == call_id,
            HumanCallSession.organization_id == organization_id,
        ),
    )
    if call_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Call session not found",
        )
    return call_session


def _require_human_call_participant(
    call_session: HumanCallSession,
    *,
    user_id: str,
) -> None:
    if user_id not in {
        call_session.caller_user_id,
        call_session.callee_user_id,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only call participants can manage this call",
        )


async def _dispatch_human_call(
    *,
    organization_id: int,
    action: Literal["incoming", "answered", "ended", "declined"],
    call_session: HumanCallSession,
) -> None:
    await dispatch_org_chat_best_effort(
        {
            "kind": "dm_call",
            "action": action,
            "organization_id": organization_id,
            "call": _human_call_event(call_session),
        },
    )


@router.post(
    "/organizations/{organization_id}/dms/{other_user_id}/calls",
    response_model=HumanCallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dm_call(
    request_fastapi: Request,
    organization_id: int,
    other_user_id: str,
    session: Session = Depends(get_db_session),
) -> HumanCallCreateResponse:
    """Start a human-to-human org DM voice call."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    _require_dm_counterpart(
        session,
        org=org,
        user_id=user_id,
        other_user_id=other_user_id,
    )

    dm_dao = DmDAO(session)
    thread = dm_dao.get_or_create_thread(
        organization_id=organization_id,
        user_id_1=user_id,
        user_id_2=other_user_id,
    )
    room_name = f"unity_org_{organization_id}_dm_{thread.id}"
    call_session = HumanCallSession(
        organization_id=organization_id,
        caller_user_id=user_id,
        callee_user_id=other_user_id,
        livekit_room=room_name,
        status="ringing",
        thread_id=thread.id,
    )
    session.add(call_session)
    session.commit()

    await _dispatch_human_call(
        organization_id=organization_id,
        action="incoming",
        call_session=call_session,
    )
    return HumanCallCreateResponse(**_human_call_response(call_session).model_dump())


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/answer",
    response_model=HumanCallSessionResponse,
)
async def answer_dm_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> HumanCallSessionResponse:
    """Mark a ringing DM call as active."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_human_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    if call_session.callee_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the callee can answer this call",
        )
    if call_session.status == "active":
        return _human_call_response(call_session)
    if call_session.status != "ringing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot answer a call with status '{call_session.status}'",
        )
    call_session.status = "active"
    call_session.answered_at = datetime.datetime.now(datetime.timezone.utc)
    session.commit()

    await _dispatch_human_call(
        organization_id=organization_id,
        action="answered",
        call_session=call_session,
    )
    return _human_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/decline",
    response_model=HumanCallSessionResponse,
)
async def decline_dm_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> HumanCallSessionResponse:
    """Decline a ringing DM call."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_human_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    if call_session.callee_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the callee can decline this call",
        )
    if call_session.status == "declined":
        return _human_call_response(call_session)
    if call_session.status != "ringing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot decline a call with status '{call_session.status}'",
        )
    call_session.status = "declined"
    call_session.ended_at = datetime.datetime.now(datetime.timezone.utc)
    session.commit()

    await _dispatch_human_call(
        organization_id=organization_id,
        action="declined",
        call_session=call_session,
    )
    return _human_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/end",
    response_model=HumanCallSessionResponse,
)
async def end_dm_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> HumanCallSessionResponse:
    """End a DM call as either participant."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_human_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    _require_human_call_participant(call_session, user_id=user_id)
    if call_session.status in {"ended", "declined"}:
        return _human_call_response(call_session)
    call_session.status = "ended"
    call_session.ended_at = datetime.datetime.now(datetime.timezone.utc)
    session.commit()

    await _dispatch_human_call(
        organization_id=organization_id,
        action="ended",
        call_session=call_session,
    )
    return _human_call_response(call_session)
