"""Org roster and chat-group management endpoints.

The roster powers the Console top selector (humans + teams + groups
alongside the assistant list). Chat *messages* live in the unified chat
store (:mod:`orchestra.web.api.chat`); call sessions live in the unified
call store (:mod:`orchestra.web.api.calls`). This module keeps the
non-message org-chat surfaces: roster and chat-group CRUD.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dao.user_presence_dao import UserPresenceDAO, presence_is_online
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Organization, Team, User
from orchestra.services.chat_group_service import (
    create_chat_group,
    delete_chat_group,
    get_active_group,
    group_to_roster_dict,
    is_human_group_member,
    list_groups_for_user,
    replace_group_membership,
)
from orchestra.services.chat_service import assistant_display_name
from orchestra.web.api.org_chat.schema import (
    ChatGroupCreate,
    ChatGroupResponse,
    ChatGroupsPage,
    ChatGroupUpdate,
    OrgRosterResponse,
    RosterAssistant,
    RosterGroup,
    RosterHuman,
    RosterTeam,
)

router = APIRouter()
logger = logging.getLogger(__name__)


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

    Private (single-player) coordinators are deliberately excluded from
    every team's ``assistant_member_ids``: their memberships exist for
    shared-memory access, not for the roster or group chat. Multiplayer
    twins participate like hired teammates and are listed.
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
            if not assistant.is_private_coordinator
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
                    user_id=assistant.user_id,
                    organization_id=assistant.organization_id,
                    desktop_mode=assistant.desktop_mode,
                    managed_desktop_status=assistant.managed_desktop_status,
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
