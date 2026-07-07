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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.dm_dao import DmDAO, normalized_pair
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dao.user_presence_dao import UserPresenceDAO, presence_is_online
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Organization, Team, User
from orchestra.services.org_chat_service import (
    SENDER_KIND_ASSISTANT,
    SENDER_KIND_USER,
    build_team_dispatch_payload,
    list_team_messages,
    persist_team_message,
)
from orchestra.web.api.org_chat.schema import (
    AssistantTeamMessageCreate,
    DmMessageCreate,
    DmMessageResponse,
    DmMessagesPage,
    OrgRosterResponse,
    RosterHuman,
    RosterTeam,
    TeamMessageCreate,
    TeamMessageResponse,
    TeamMessagesPage,
)
from orchestra.web.api.utils.assistant_infra import dispatch_org_chat_best_effort

router = APIRouter()
admin_router = APIRouter()


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
        messages=[TeamMessageResponse(**message) for message in messages],
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
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    payload = build_team_dispatch_payload(
        session,
        team=team,
        message=message,
        fan_out=True,
        sender_email=sender.email or "",
    )
    await dispatch_org_chat_best_effort(payload)
    return TeamMessageResponse(**message)


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
    """Assistant runtime posting a group-chat reply (admin auth).

    The reply is persisted and published to the Console stream but never
    fans out to other assistant runtimes — assistants only ever trigger on
    human messages, which mechanically prevents AI reply loops.
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
        assistant_id=body.assistant_id,
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assistant is not a member of this team",
        )
    assistant = team_dao.get_assistant(body.assistant_id)
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
            content=body.content,
            mentions=[mention.model_dump() for mention in body.mentions],
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    payload = build_team_dispatch_payload(
        session,
        team=team,
        message=message,
        fan_out=False,
    )
    await dispatch_org_chat_best_effort(payload)
    return TeamMessageResponse(**message)


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
    )
    return DmMessagesPage(
        thread_id=thread.id,
        organization_id=organization_id,
        user_ids=list(normalized_pair(user_id, other_user_id)),
        messages=[
            DmMessageResponse(
                id=message.id,
                thread_id=message.thread_id,
                sender_user_id=message.sender_user_id,
                content=message.content,
                created_at=message.created_at,
            )
            for message in messages
        ],
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
    )
    session.commit()

    await dispatch_org_chat_best_effort(
        {
            "kind": "dm",
            "organization_id": organization_id,
            "message": {
                "id": message.id,
                "thread_id": thread.id,
                "organization_id": organization_id,
                "user_ids": list(normalized_pair(user_id, other_user_id)),
                "sender_user_id": user_id,
                "sender_name": _display_name(sender),
                "content": message.content,
                "timestamp": message.created_at.isoformat(),
            },
        },
    )

    return DmMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        sender_user_id=message.sender_user_id,
        content=message.content,
        created_at=message.created_at,
    )
