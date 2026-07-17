"""Org roster, chat-group management, and org call endpoints.

The roster powers the Console top selector (humans + teams + groups
alongside the assistant list). Chat *messages* for every surface (human DM,
assistant DM, team, group) live in the unified chat store and are served by
the ``/chat`` API (:mod:`orchestra.web.api.chat`); this module keeps the
non-message org-chat surfaces: roster, chat-group CRUD, and multi-party org
call sessions.
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
from orchestra.db.dao.user_presence_dao import UserPresenceDAO, presence_is_online
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrgCallParticipant,
    OrgCallSession,
    Team,
    User,
)
from orchestra.services.chat_group_service import (
    create_chat_group,
    delete_chat_group,
    get_active_group,
    group_to_roster_dict,
    is_assistant_group_member,
    is_human_group_member,
    list_groups_for_user,
    replace_group_membership,
)
from orchestra.services.chat_service import assistant_display_name
from orchestra.services.org_call_contacts import ensure_org_call_contacts
from orchestra.web.api.org_chat.schema import (
    ChatGroupCreate,
    ChatGroupResponse,
    ChatGroupsPage,
    ChatGroupUpdate,
    OrgCallActiveListResponse,
    OrgCallAddAssistantRequest,
    OrgCallCreateResponse,
    OrgCallParticipantResponse,
    OrgCallRosterMember,
    OrgCallSessionResponse,
    OrgRosterResponse,
    RosterAssistant,
    RosterGroup,
    RosterHuman,
    RosterTeam,
)
from orchestra.web.api.utils.assistant_infra import (
    ADAPTERS_URL,
    ADMIN_KEY,
    LOCAL_ADAPTERS_URL,
    dispatch_chat_best_effort,
)
from orchestra.web.api.utils.http_client import get_async_client

router = APIRouter()
logger = logging.getLogger(__name__)


def _org_call_event(call_session: OrgCallSession) -> dict[str, Any]:
    participants = list(call_session.participants or [])
    user_ids = [p.user_id for p in participants]
    callee_user_id = None
    if call_session.scope == "dm":
        for participant in participants:
            if participant.user_id != call_session.created_by_user_id:
                callee_user_id = participant.user_id
                break
    return {
        "call_id": call_session.id,
        "room_name": call_session.livekit_room,
        "status": call_session.status,
        "scope": call_session.scope,
        "created_by_user_id": call_session.created_by_user_id,
        "caller_user_id": call_session.created_by_user_id,
        "callee_user_id": callee_user_id,
        "organization_id": call_session.organization_id,
        "dm_thread_id": call_session.thread_id,
        "thread_id": call_session.thread_id,
        "team_id": call_session.team_id,
        "group_id": call_session.group_id,
        "user_ids": user_ids,
        "assistant_ids": list(call_session.assistant_ids or []),
        "participants": [
            {
                "user_id": participant.user_id,
                "role": participant.role,
                "status": participant.status,
            }
            for participant in participants
        ],
    }


def _display_roster(
    session: Session,
    call_session: OrgCallSession,
) -> list[OrgCallRosterMember]:
    """Display names/emails for everyone on the call.

    Purely informational (no Contacts upserts) — the per-assistant contact
    roster used for Meet dispatch is built by ``ensure_org_call_contacts``.
    """
    roster: list[OrgCallRosterMember] = []
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
                OrgCallRosterMember(
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
                OrgCallRosterMember(
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


def _org_call_response(call_session: OrgCallSession) -> OrgCallSessionResponse:
    event = _org_call_event(call_session)
    session = object_session(call_session)
    return OrgCallSessionResponse(
        call_id=event["call_id"],
        room_name=event["room_name"],
        status=event["status"],
        scope=event["scope"],
        created_by_user_id=event["created_by_user_id"],
        caller_user_id=event["caller_user_id"],
        callee_user_id=event.get("callee_user_id"),
        team_id=event.get("team_id"),
        group_id=event.get("group_id"),
        thread_id=event.get("thread_id"),
        dm_thread_id=event.get("dm_thread_id"),
        user_ids=event["user_ids"],
        participants=[
            OrgCallParticipantResponse(**participant)
            for participant in event["participants"]
        ],
        assistant_ids=event["assistant_ids"],
        roster=_display_roster(session, call_session) if session is not None else [],
    )


def _participant_for_user(
    call_session: OrgCallSession,
    *,
    user_id: str,
) -> OrgCallParticipant | None:
    for participant in call_session.participants or []:
        if participant.user_id == user_id:
            return participant
    return None


def _joined_human_count(call_session: OrgCallSession) -> int:
    return sum(
        1
        for participant in call_session.participants or []
        if participant.status == "joined"
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

    groups: list[RosterGroup] = []
    for group in list_groups_for_user(
        session,
        organization_id=organization_id,
        user_id=user_id,
    ):
        groups.append(RosterGroup(**group_to_roster_dict(session, group)))

    # Assistant directory: everyone reachable through a team or group, so
    # call tiles and pickers can resolve names without a per-page assistant
    # list fetch.
    assistant_ids: set[int] = set()
    for team in teams:
        assistant_ids.update(team.assistant_member_ids)
    for group in groups:
        assistant_ids.update(group.assistant_member_ids)
    assistants: list[RosterAssistant] = []
    if assistant_ids:
        for assistant in session.scalars(
            select(Assistant).where(Assistant.agent_id.in_(assistant_ids)),
        ).all():
            assistants.append(
                RosterAssistant(
                    assistant_id=assistant.agent_id,
                    name=assistant_display_name(assistant),
                ),
            )

    return OrgRosterResponse(
        organization_id=organization_id,
        humans=humans,
        teams=teams,
        groups=groups,
        assistants=assistants,
    )


def _chat_group_response(session: Session, group) -> ChatGroupResponse:
    roster = group_to_roster_dict(session, group)
    return ChatGroupResponse(
        group_id=roster["group_id"],
        name=roster["name"],
        organization_id=group.organization_id,
        created_by_user_id=roster["created_by_user_id"],
        created_at=roster["created_at"],
        member_user_ids=roster["member_user_ids"],
        assistant_member_ids=roster["assistant_member_ids"],
    )


def _require_active_group(
    session: Session,
    *,
    organization_id: int,
    group_id: int,
):
    group = get_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chat group with id {group_id} not found",
        )
    return group


def _require_human_group_member(
    session: Session,
    *,
    group_id: int,
    user_id: str,
) -> None:
    if not is_human_group_member(session, group_id=group_id, user_id=user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this group",
        )


def _validate_group_member_ids(
    session: Session,
    *,
    org: Organization,
    user_ids: list[str],
    assistant_ids: list[int],
) -> None:
    for member_user_id in user_ids:
        _require_org_member(session, org=org, user_id=member_user_id)
    for assistant_id in assistant_ids:
        assistant = session.get(Assistant, assistant_id)
        if assistant is None or assistant.organization_id != org.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Assistant {assistant_id} is not in this organization",
            )
        if assistant.is_coordinator:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Coordinators cannot be added to chat groups",
            )


@router.get(
    "/organizations/{organization_id}/groups",
    response_model=ChatGroupsPage,
)
def list_org_groups(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> ChatGroupsPage:
    """List chat groups the authenticated human belongs to."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    groups = list_groups_for_user(
        session,
        organization_id=organization_id,
        user_id=user_id,
    )
    return ChatGroupsPage(
        groups=[_chat_group_response(session, group) for group in groups],
    )


@router.post(
    "/organizations/{organization_id}/groups",
    response_model=ChatGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_org_group(
    request_fastapi: Request,
    organization_id: int,
    body: ChatGroupCreate,
    session: Session = Depends(get_db_session),
) -> ChatGroupResponse:
    """Create a chat group; the creator is always included as a human member."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    _validate_group_member_ids(
        session,
        org=org,
        user_ids=body.user_ids,
        assistant_ids=body.assistant_ids,
    )
    group = create_chat_group(
        session,
        organization_id=organization_id,
        created_by_user_id=user_id,
        name=body.name,
        user_ids=body.user_ids,
        assistant_ids=body.assistant_ids,
    )
    session.commit()
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group.id,
    )
    return _chat_group_response(session, group)


@router.get(
    "/organizations/{organization_id}/groups/{group_id}",
    response_model=ChatGroupResponse,
)
def get_org_group(
    request_fastapi: Request,
    organization_id: int,
    group_id: int,
    session: Session = Depends(get_db_session),
) -> ChatGroupResponse:
    """Return one chat group the caller belongs to."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    _require_human_group_member(session, group_id=group_id, user_id=user_id)
    return _chat_group_response(session, group)


@router.patch(
    "/organizations/{organization_id}/groups/{group_id}",
    response_model=ChatGroupResponse,
)
def update_org_group(
    request_fastapi: Request,
    organization_id: int,
    group_id: int,
    body: ChatGroupUpdate,
    session: Session = Depends(get_db_session),
) -> ChatGroupResponse:
    """Rename and/or replace membership for a chat group."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    _require_human_group_member(session, group_id=group_id, user_id=user_id)

    if body.name is not None:
        trimmed = body.name.strip()
        if not trimmed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Group name cannot be empty",
            )
        group.name = trimmed

    if body.user_ids is not None or body.assistant_ids is not None:
        next_user_ids = (
            body.user_ids
            if body.user_ids is not None
            else [m.user_id for m in (group.members or []) if m.user_id]
        )
        next_assistant_ids = (
            body.assistant_ids
            if body.assistant_ids is not None
            else [
                m.assistant_id
                for m in (group.members or [])
                if m.assistant_id is not None
            ]
        )
        _validate_group_member_ids(
            session,
            org=org,
            user_ids=next_user_ids,
            assistant_ids=next_assistant_ids,
        )
        group = replace_group_membership(
            session,
            group=group,
            user_ids=next_user_ids,
            assistant_ids=next_assistant_ids,
        )

    session.commit()
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    return _chat_group_response(session, group)


@router.delete(
    "/organizations/{organization_id}/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_org_group(
    request_fastapi: Request,
    organization_id: int,
    group_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    """Soft-delete a chat group and purge its GroupChat contexts."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    _require_human_group_member(session, group_id=group_id, user_id=user_id)
    delete_chat_group(session, group=group)
    session.commit()


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


def _require_org_call_session(
    session: Session,
    *,
    organization_id: int,
    call_id: str,
) -> OrgCallSession:
    call_session = session.scalar(
        select(OrgCallSession).where(
            OrgCallSession.id == call_id,
            OrgCallSession.organization_id == organization_id,
        ),
    )
    if call_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Call session not found",
        )
    # Ensure participants are loaded for response/event payloads.
    _ = list(call_session.participants)
    return call_session


def _require_org_call_participant(
    call_session: OrgCallSession,
    *,
    user_id: str,
) -> OrgCallParticipant:
    participant = _participant_for_user(call_session, user_id=user_id)
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only call participants can manage this call",
        )
    return participant


async def _dispatch_org_call(
    *,
    organization_id: int,
    action: Literal[
        "incoming",
        "answered",
        "ended",
        "declined",
        "participant_joined",
        "participant_left",
    ],
    call_session: OrgCallSession,
) -> None:
    await dispatch_chat_best_effort(
        {
            "kind": "org_call",
            "action": action,
            "organization_id": organization_id,
            "call": _org_call_event(call_session),
        },
    )


def _roster_payload(members: list[OrgCallRosterMember]) -> list[dict[str, Any]]:
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


async def _refresh_org_call_assistant_rosters(
    session: Session,
    *,
    call_session: OrgCallSession,
    assistant_ids: list[int],
) -> None:
    """Re-ensure Contacts and push updated participants to running assistants."""
    adapters_url = (LOCAL_ADAPTERS_URL or ADAPTERS_URL or "").rstrip("/")
    if not adapters_url or not ADMIN_KEY or not assistant_ids:
        return
    client = get_async_client()
    for assistant_id in assistant_ids:
        roster = ensure_org_call_contacts(
            session,
            call_session=call_session,
            for_assistant_id=assistant_id,
        )
        session.commit()
        try:
            await client.post(
                f"{adapters_url}/unify/meet",
                headers={
                    "Authorization": f"Bearer {ADMIN_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "assistant_id": str(assistant_id),
                    "room_name": call_session.livekit_room,
                    "call_session_id": call_session.id,
                    "participants": _roster_payload(roster),
                },
            )
        except Exception:
            logger.exception(
                "Failed to refresh Unify Meet roster for assistant %s on call %s",
                assistant_id,
                call_session.id,
            )


def _finalize_org_call_room_name(
    session: Session,
    call_session: OrgCallSession,
) -> None:
    session.flush()
    call_session.livekit_room = (
        f"unity_org_{call_session.organization_id}_call_{call_session.id}"
    )


@router.post(
    "/organizations/{organization_id}/dms/{other_user_id}/calls",
    response_model=OrgCallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dm_call(
    request_fastapi: Request,
    organization_id: int,
    other_user_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallCreateResponse:
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

    thread = ChatDAO(session).resolve_dm_thread(
        organization_id=organization_id,
        user_id_1=user_id,
        user_id_2=other_user_id,
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    call_session = OrgCallSession(
        organization_id=organization_id,
        scope="dm",
        thread_id=thread.id,
        team_id=None,
        created_by_user_id=user_id,
        livekit_room="pending",
        status="ringing",
        assistant_ids=[],
    )
    session.add(call_session)
    _finalize_org_call_room_name(session, call_session)
    session.add(
        OrgCallParticipant(
            call_id=call_session.id,
            user_id=user_id,
            role="host",
            status="joined",
            joined_at=now,
        ),
    )
    session.add(
        OrgCallParticipant(
            call_id=call_session.id,
            user_id=other_user_id,
            role="member",
            status="invited",
        ),
    )
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_session.id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="incoming",
        call_session=call_session,
    )
    return OrgCallCreateResponse(**_org_call_response(call_session).model_dump())


@router.post(
    "/organizations/{organization_id}/teams/{team_id}/calls",
    response_model=OrgCallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_team_call(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    session: Session = Depends(get_db_session),
) -> OrgCallCreateResponse:
    """Start a multi-party team call and ring every human team member."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    team = session.get(Team, team_id)
    if team is None or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )
    team_dao = TeamDAO(session)
    if not team_dao.is_team_member(team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this team to start a call",
        )

    member_ids = team_dao.get_team_members(team_id)
    if user_id not in member_ids:
        member_ids = [*member_ids, user_id]
    if len(member_ids) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Team has no members to call",
        )

    thread = ChatDAO(session).resolve_team_thread(
        team_id=team_id,
        organization_id=organization_id,
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    call_session = OrgCallSession(
        organization_id=organization_id,
        scope="team",
        thread_id=thread.id,
        team_id=team_id,
        created_by_user_id=user_id,
        livekit_room="pending",
        status="ringing",
        assistant_ids=[],
    )
    session.add(call_session)
    _finalize_org_call_room_name(session, call_session)
    for member_id in member_ids:
        is_host = member_id == user_id
        session.add(
            OrgCallParticipant(
                call_id=call_session.id,
                user_id=member_id,
                role="host" if is_host else "member",
                status="joined" if is_host else "invited",
                joined_at=now if is_host else None,
            ),
        )
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_session.id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="incoming",
        call_session=call_session,
    )
    return OrgCallCreateResponse(**_org_call_response(call_session).model_dump())


@router.post(
    "/organizations/{organization_id}/groups/{group_id}/calls",
    response_model=OrgCallCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_group_call(
    request_fastapi: Request,
    organization_id: int,
    group_id: int,
    session: Session = Depends(get_db_session),
) -> OrgCallCreateResponse:
    """Start a multi-party group call and ring every human group member."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    group = _require_active_group(
        session,
        organization_id=organization_id,
        group_id=group_id,
    )
    _require_human_group_member(session, group_id=group_id, user_id=user_id)

    member_ids = [m.user_id for m in (group.members or []) if m.user_id]
    if user_id not in member_ids:
        member_ids = [*member_ids, user_id]
    if len(member_ids) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Group has no members to call",
        )

    thread = ChatDAO(session).resolve_group_thread(
        group_id=group_id,
        organization_id=organization_id,
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    call_session = OrgCallSession(
        organization_id=organization_id,
        scope="group",
        thread_id=thread.id,
        team_id=None,
        group_id=group_id,
        created_by_user_id=user_id,
        livekit_room="pending",
        status="ringing",
        assistant_ids=[],
    )
    session.add(call_session)
    _finalize_org_call_room_name(session, call_session)
    for member_id in member_ids:
        is_host = member_id == user_id
        session.add(
            OrgCallParticipant(
                call_id=call_session.id,
                user_id=member_id,
                role="host" if is_host else "member",
                status="joined" if is_host else "invited",
                joined_at=now if is_host else None,
            ),
        )
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_session.id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="incoming",
        call_session=call_session,
    )
    return OrgCallCreateResponse(**_org_call_response(call_session).model_dump())


@router.get(
    "/organizations/{organization_id}/calls/active",
    response_model=OrgCallActiveListResponse,
)
def list_active_org_calls(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> OrgCallActiveListResponse:
    """Live (ringing/active) calls that include the caller as a participant.

    Powers the Console rejoin banner after a page reload: the app-level call
    engine re-attaches to any call the user was on.
    """
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_sessions = (
        session.scalars(
            select(OrgCallSession)
            .join(
                OrgCallParticipant,
                OrgCallParticipant.call_id == OrgCallSession.id,
            )
            .where(
                OrgCallSession.organization_id == organization_id,
                OrgCallSession.status.in_(["ringing", "active"]),
                OrgCallParticipant.user_id == user_id,
            )
            .order_by(OrgCallSession.created_at.desc()),
        )
        .unique()
        .all()
    )
    return OrgCallActiveListResponse(
        calls=[_org_call_response(call_session) for call_session in call_sessions],
    )


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/answer",
    response_model=OrgCallSessionResponse,
)
async def answer_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """Mark an invited participant as joined and activate the call."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    participant = _require_org_call_participant(call_session, user_id=user_id)
    if participant.status == "joined" and call_session.status == "active":
        return _org_call_response(call_session)
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
        call_session.answered_at = now
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="answered",
        call_session=call_session,
    )
    if call_session.assistant_ids:
        await _refresh_org_call_assistant_rosters(
            session,
            call_session=call_session,
            assistant_ids=[int(a) for a in call_session.assistant_ids],
        )
    return _org_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/join",
    response_model=OrgCallSessionResponse,
)
async def join_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """Late-join an active org call when the user is an invitee or team/group member."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
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
            participant = OrgCallParticipant(
                call_id=call_session.id,
                user_id=user_id,
                role="member",
                status="invited",
            )
            session.add(participant)
            session.flush()
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
            participant = OrgCallParticipant(
                call_id=call_session.id,
                user_id=user_id,
                role="member",
                status="invited",
            )
            session.add(participant)
            session.flush()
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only call invitees can join this call",
            )

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "joined"
    participant.joined_at = participant.joined_at or now
    participant.left_at = None
    if call_session.status == "ringing":
        call_session.status = "active"
        call_session.answered_at = now
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="participant_joined",
        call_session=call_session,
    )
    if call_session.assistant_ids:
        await _refresh_org_call_assistant_rosters(
            session,
            call_session=call_session,
            assistant_ids=[int(a) for a in call_session.assistant_ids],
        )
    return _org_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/decline",
    response_model=OrgCallSessionResponse,
)
async def decline_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """Decline a ringing invite. DM declines end the call; team declines are per-user."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    participant = _require_org_call_participant(call_session, user_id=user_id)
    if participant.role == "host":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Host cannot decline; end the call instead",
        )
    if participant.status == "declined" and call_session.scope == "team":
        return _org_call_response(call_session)
    if call_session.status == "ended":
        return _org_call_response(call_session)
    if participant.status not in {"invited", "declined"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot decline with participant status '{participant.status}'",
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "declined"
    participant.left_at = now

    action: Literal["declined", "ended"] = "declined"
    if call_session.scope == "dm":
        call_session.status = "ended"
        call_session.ended_at = now
        action = "ended"
    else:
        # End the team call only when every non-host invitee has declined and
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
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action=action,
        call_session=call_session,
    )
    return _org_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/leave",
    response_model=OrgCallSessionResponse,
)
async def leave_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """Leave an active call without ending it for remaining humans."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    participant = _require_org_call_participant(call_session, user_id=user_id)
    if call_session.status == "ended":
        return _org_call_response(call_session)

    now = datetime.datetime.now(datetime.timezone.utc)
    participant.status = "left"
    participant.left_at = now
    session.flush()

    action: Literal["participant_left", "ended"] = "participant_left"
    if _joined_human_count(call_session) == 0:
        call_session.status = "ended"
        call_session.ended_at = now
        action = "ended"
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action=action,
        call_session=call_session,
    )
    return _org_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/end",
    response_model=OrgCallSessionResponse,
)
async def end_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """End an org call for every participant."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    participant = _require_org_call_participant(call_session, user_id=user_id)
    is_host = participant.role == "host"
    is_last_human = (
        _joined_human_count(call_session) <= 1 and participant.status == "joined"
    )
    if not is_host and not is_last_human and call_session.status != "ended":
        # Non-hosts may end only when they are the last joined human.
        if participant.status != "joined":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the host can end this call for everyone",
            )
    if call_session.status == "ended":
        return _org_call_response(call_session)

    now = datetime.datetime.now(datetime.timezone.utc)
    call_session.status = "ended"
    call_session.ended_at = now
    for part in call_session.participants:
        if part.status == "joined":
            part.status = "left"
            part.left_at = now
    session.commit()
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )

    await _dispatch_org_call(
        organization_id=organization_id,
        action="ended",
        call_session=call_session,
    )
    return _org_call_response(call_session)


@router.post(
    "/organizations/{organization_id}/calls/{call_id}/assistants",
    response_model=OrgCallSessionResponse,
)
async def add_assistant_to_org_call(
    request_fastapi: Request,
    organization_id: int,
    call_id: str,
    body: OrgCallAddAssistantRequest,
    session: Session = Depends(get_db_session),
) -> OrgCallSessionResponse:
    """Record an assistant on an active team or group call (Console dispatches Meet)."""
    user_id = request_fastapi.state.user_id
    org = _require_org(session, organization_id)
    _require_org_member(session, org=org, user_id=user_id)
    call_session = _require_org_call_session(
        session,
        organization_id=organization_id,
        call_id=call_id,
    )
    _require_org_call_participant(call_session, user_id=user_id)
    if call_session.status not in {"ringing", "active"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot add an assistant to a finished call",
        )
    if call_session.scope == "team":
        if call_session.team_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Assistants can only be added to team or group calls",
            )
        membership = TeamDAO(session).get_assistant_membership(
            team_id=call_session.team_id,
            assistant_id=body.assistant_id,
        )
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Assistant must be a member of this team",
            )
    elif call_session.scope == "group":
        if call_session.group_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Assistants can only be added to team or group calls",
            )
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assistants can only be added to team or group calls",
        )

    assistant_ids = list(call_session.assistant_ids or [])
    if body.assistant_id not in assistant_ids:
        assistant_ids.append(body.assistant_id)
        call_session.assistant_ids = assistant_ids
        session.commit()
        call_session = _require_org_call_session(
            session,
            organization_id=organization_id,
            call_id=call_id,
        )

    roster = ensure_org_call_contacts(
        session,
        call_session=call_session,
        for_assistant_id=body.assistant_id,
    )
    session.commit()

    await _dispatch_org_call(
        organization_id=organization_id,
        action="participant_joined",
        call_session=call_session,
    )
    # Push refreshed peer rosters to assistants already on the call.
    peer_ids = [
        int(a)
        for a in (call_session.assistant_ids or [])
        if int(a) != body.assistant_id
    ]
    if peer_ids:
        await _refresh_org_call_assistant_rosters(
            session,
            call_session=call_session,
            assistant_ids=peer_ids,
        )
    response = _org_call_response(call_session)
    response.roster = roster
    return response
