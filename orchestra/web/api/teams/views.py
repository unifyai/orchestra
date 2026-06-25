"""Team management endpoints."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.team_dao import TEAM_STATUS_ACTIVE, TeamDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Team
from orchestra.services.contact_membership_service import (
    ensure_team_contact_memberships,
)
from orchestra.services.coordinator_service import (
    ensure_workspace_coordinator_provisioned,
    get_workspace_coordinator,
)
from orchestra.services.org_wide_sharing_service import (
    WorkspaceCoordinatorProvisioningError,
    add_assistant_to_team,
    add_coordinator_to_team,
)
from orchestra.services.team_cleanup_service import (
    TeamCleanupAuthError,
    TeamCleanupConflictError,
    TeamCleanupFailure,
    TeamCleanupNotFoundError,
)
from orchestra.services.team_cleanup_service import delete_team as run_team_cleanup
from orchestra.services.team_cleanup_service import (
    purge_assistant_overlay as purge_team_member_overlay,
)
from orchestra.services.team_membership_refresh_service import (
    membership_refresh_payloads,
    publish_membership_refreshes_best_effort,
)
from orchestra.web.api.teams.schema import (
    ResourceAccessGrant,
    ResourceAccessListResponse,
    ResourceAccessResponse,
    ResourceAccessRevoke,
    ResourceAccessUpdate,
    TeamAssistantMember,
    TeamAssistantMemberCreate,
    TeamCreate,
    TeamMemberAdd,
    TeamMembershipResponse,
    TeamMembershipStatus,
    TeamResponse,
    TeamSummary,
    TeamUpdate,
    TeamWithMembersResponse,
    UserResourceAccessEntry,
    UserResourceAccessResponse,
)
from orchestra.web.api.utils.assistant_infra import delete_pubsub_topic

router = APIRouter()


def _require_active_team(team: Team) -> None:
    """Reject mutations against teams that are being deleted."""

    if team.status != TEAM_STATUS_ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="team_not_active",
        )


def _require_unmanaged_team(team: Team) -> None:
    """Reject manual mutations against the managed org-wide sharing team."""

    if team.is_org_wide_sharing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="org_wide_sharing_team_managed",
        )


def _ensure_member_team_contacts(
    session: Session,
    *,
    assistant_id: int,
    team_id: int,
) -> None:
    """Ensure default team-scoped self/boss overlays for one live membership."""

    ensure_team_contact_memberships(session, [(assistant_id, team_id)])


async def _add_coordinator_to_team(
    session: Session,
    *,
    team: Team,
    member_user_id: str,
    actor_user_id: str,
) -> tuple[Assistant, bool, list]:
    """Provision a member's workspace coordinator and add it to the team."""

    try:
        assistant, result = await add_coordinator_to_team(
            session,
            team=team,
            member_user_id=member_user_id,
            actor_user_id=actor_user_id,
        )
    except WorkspaceCoordinatorProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workspace_coordinator_provisioning_failed",
        ) from exc

    return assistant, bool(result.created_coordinator_ids), result.refresh_payloads


def _require_assistant_membership_target_allowed(
    *,
    actor_user_id: str,
    team: Team,
    assistant: Assistant,
) -> None:
    """Require the target assistant to be eligible for the team."""

    if assistant.organization_id == team.organization_id:
        return
    if assistant.user_id == actor_user_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="assistant_not_eligible_for_team",
    )


@router.post(
    "/organizations/{organization_id}/teams",
    response_model=TeamResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_team(
    request_fastapi: Request,
    organization_id: int,
    team_data: TeamCreate,
    session: Session = Depends(get_db_session),
) -> TeamResponse:
    """
    Create a new team in an organization.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_data: Team creation data.
    :param session: Database session.
    :return: Created team.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create teams in this organization",
        )

    # Check for duplicate team name
    existing_team = team_dao.get_by_name(team_data.name, organization_id)
    if existing_team:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Team with name '{team_data.name}' already exists in this organization",
        )

    created_workspace_coordinator = False
    created_workspace_coordinator_id: int | None = None
    refresh_payloads = []
    try:
        team = team_dao.create(
            name=team_data.name,
            organization_id=organization_id,
            description=team_data.description,
        )
        if not team_dao.is_team_member(team.id, user_id):
            team_dao.add_member(team.id, user_id)
        coordinator = get_workspace_coordinator(
            session,
            user_id=user_id,
            organization_id=organization_id,
        )
        if coordinator is None:
            try:
                coordinator, created_workspace_coordinator = (
                    await ensure_workspace_coordinator_provisioned(
                        session,
                        user_id=user_id,
                        organization_id=organization_id,
                    )
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="workspace_coordinator_provisioning_failed",
                ) from exc
            created_workspace_coordinator_id = coordinator.agent_id
        if (
            team_dao.get_assistant_membership(
                team_id=team.id,
                assistant_id=coordinator.agent_id,
            )
            is None
        ):
            team_dao.add_assistant_membership(
                team=team,
                assistant=coordinator,
                added_by=user_id,
            )
            _ensure_member_team_contacts(
                session,
                assistant_id=coordinator.agent_id,
                team_id=team.id,
            )
            refresh_payloads = membership_refresh_payloads(session, [coordinator])
        session.commit()
    except HTTPException:
        session.rollback()
        if (
            created_workspace_coordinator
            and created_workspace_coordinator_id is not None
        ):
            await delete_pubsub_topic(str(created_workspace_coordinator_id))
        raise
    except Exception as e:
        session.rollback()
        if (
            created_workspace_coordinator
            and created_workspace_coordinator_id is not None
        ):
            await delete_pubsub_topic(str(created_workspace_coordinator_id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create team: {str(e)}",
        )

    await publish_membership_refreshes_best_effort(refresh_payloads)

    return TeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        organization_id=team.organization_id,
        created_at=team.created_at,
        member_count=len(team_dao.get_team_members(team.id)),
        is_org_wide_sharing=team.is_org_wide_sharing,
    )


@router.get(
    "/organizations/{organization_id}/teams",
    response_model=List[TeamResponse],
    status_code=status.HTTP_200_OK,
)
def list_teams(
    request_fastapi: Request,
    organization_id: int,
    session: Session = Depends(get_db_session),
) -> List[TeamResponse]:
    """
    List all teams in an organization.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param session: Database session.
    :return: List of teams.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is a member
    is_owner = org.owner_id == user_id
    is_member = org_member_dao.filter(user_id=user_id, organization_id=organization_id)

    if not is_owner and not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this organization to view teams",
        )

    teams = team_dao.list_organization_teams(organization_id)

    return [
        TeamResponse(
            id=team.id,
            name=team.name,
            description=team.description,
            organization_id=team.organization_id,
            created_at=team.created_at,
            members=(members := team_dao.get_team_members(team.id)),
            member_count=len(members),
            is_org_wide_sharing=team.is_org_wide_sharing,
        )
        for team in teams
    ]


@router.get(
    "/organizations/{organization_id}/teams/{team_id}",
    response_model=TeamWithMembersResponse,
    status_code=status.HTTP_200_OK,
)
def get_team(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    session: Session = Depends(get_db_session),
) -> TeamWithMembersResponse:
    """
    Get team details including members.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param session: Database session.
    :return: Team details with members.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Get team
    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    # Check if user is a member
    is_owner = org.owner_id == user_id
    is_member = org_member_dao.filter(user_id=user_id, organization_id=organization_id)

    if not is_owner and not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this organization to view teams",
        )

    members = team_dao.get_team_members(team_id)

    return TeamWithMembersResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        organization_id=team.organization_id,
        created_at=team.created_at,
        members=members,
        is_org_wide_sharing=team.is_org_wide_sharing,
    )


@router.patch(
    "/organizations/{organization_id}/teams/{team_id}",
    response_model=TeamResponse,
    status_code=status.HTTP_200_OK,
)
async def update_team(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    team_data: TeamUpdate,
    session: Session = Depends(get_db_session),
) -> TeamResponse:
    """
    Update a team.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param team_data: Team update data.
    :param session: Database session.
    :return: Updated team.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update teams in this organization",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_active_team(team)
    _require_unmanaged_team(team)

    if team_data.name and team_data.name != team.name:
        existing_team = team_dao.get_by_name(team_data.name, organization_id)
        if existing_team:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Team with name '{team_data.name}' already exists in this organization",
            )

    try:
        team_dao.update(
            id=team_id,
            name=team_data.name,
            description=team_data.description,
        )
        refresh_payloads = []
        if team_data.name is not None or team_data.description is not None:
            refresh_payloads = membership_refresh_payloads(
                session,
                [
                    assistant
                    for _, assistant in team_dao.list_assistant_members(team_id)
                ],
            )
        session.commit()
        team = team_dao.get(team_id)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update team: {str(e)}",
        )

    await publish_membership_refreshes_best_effort(refresh_payloads)

    return TeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        organization_id=team.organization_id,
        created_at=team.created_at,
        member_count=len(team_dao.get_team_members(team_id)),
        is_org_wide_sharing=team.is_org_wide_sharing,
    )


@router.delete(
    "/organizations/{organization_id}/teams/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_team(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    session: Session = Depends(get_db_session),
) -> Response:
    """
    Delete a team.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param session: Database session.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete teams in this organization",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_unmanaged_team(team)

    try:
        await run_team_cleanup(
            session,
            team_id=team_id,
            user_id=user_id,
            organization_id=organization_id,
        )
    except TeamCleanupNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    except TeamCleanupAuthError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="team_mutation_forbidden",
        )
    except TeamCleanupConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="team_cleanup_in_progress",
        )
    except TeamCleanupFailure as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"phase": exc.phase, "reason": exc.reason},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{organization_id}/teams/{team_id}/members",
    response_model=TeamWithMembersResponse,
    status_code=status.HTTP_200_OK,
)
async def add_team_members(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    member_data: TeamMemberAdd,
    session: Session = Depends(get_db_session),
) -> TeamWithMembersResponse:
    """
    Add members to a team.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param member_data: Members to add.
    :param session: Database session.
    :return: Updated team with members.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    user_dao = UserDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage team members",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_active_team(team)
    _require_unmanaged_team(team)

    created_coordinator_ids: list[int] = []
    refresh_payloads = []
    try:
        for user_id_to_add in member_data.user_ids:
            user = user_dao.get_by_id(user_id_to_add)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User with id {user_id_to_add} not found",
                )

            is_owner = org.owner_id == user_id_to_add
            is_member = org_member_dao.filter(
                user_id=user_id_to_add,
                organization_id=organization_id,
            )

            if not is_owner and not is_member:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"User {user_id_to_add} is not a member of this organization",
                )

            if not team_dao.is_team_member(team_id, user_id_to_add):
                team_dao.add_member(team_id, user_id_to_add)

            assistant, created, payloads = await _add_coordinator_to_team(
                session,
                team=team,
                member_user_id=user_id_to_add,
                actor_user_id=user_id,
            )
            if created:
                created_coordinator_ids.append(assistant.agent_id)
            refresh_payloads.extend(payloads)

        session.commit()
    except HTTPException:
        session.rollback()
        for coordinator_id in created_coordinator_ids:
            await delete_pubsub_topic(str(coordinator_id))
        raise
    except Exception as e:
        session.rollback()
        for coordinator_id in created_coordinator_ids:
            await delete_pubsub_topic(str(coordinator_id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add team members: {str(e)}",
        )

    await publish_membership_refreshes_best_effort(refresh_payloads)

    members = team_dao.get_team_members(team_id)
    return TeamWithMembersResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        organization_id=team.organization_id,
        created_at=team.created_at,
        members=members,
        is_org_wide_sharing=team.is_org_wide_sharing,
    )


@router.delete(
    "/organizations/{organization_id}/teams/{team_id}/members/{user_id_to_remove}",
    response_model=TeamWithMembersResponse,
    status_code=status.HTTP_200_OK,
)
async def remove_team_member(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    user_id_to_remove: str,
    session: Session = Depends(get_db_session),
) -> TeamWithMembersResponse:
    """
    Remove a member from a team.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param user_id_to_remove: User ID to remove.
    :param session: Database session.
    :return: Updated team with members.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user has org:write permission via org membership role
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage team members",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_active_team(team)
    _require_unmanaged_team(team)

    refresh_payloads = []
    try:
        coordinator = get_workspace_coordinator(
            session,
            user_id=user_id_to_remove,
            organization_id=organization_id,
        )
        if coordinator is not None and team_dao.get_assistant_membership(
            team_id=team_id,
            assistant_id=coordinator.agent_id,
        ):
            await purge_team_member_overlay(
                session,
                assistant_id=coordinator.agent_id,
                team_id=team_id,
            )
            refresh_payloads = membership_refresh_payloads(session, [coordinator])

        team_dao.remove_member(team_id, user_id_to_remove)
        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove team member: {str(e)}",
        )

    await publish_membership_refreshes_best_effort(refresh_payloads)

    members = team_dao.get_team_members(team_id)
    return TeamWithMembersResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        organization_id=team.organization_id,
        created_at=team.created_at,
        members=members,
        is_org_wide_sharing=team.is_org_wide_sharing,
    )


@router.post(
    "/organizations/{organization_id}/teams/{team_id}/assistant-members",
    response_model=TeamMembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_team_assistant_member(
    request_fastapi: Request,
    response: Response,
    organization_id: int,
    team_id: int,
    body: TeamAssistantMemberCreate,
    session: Session = Depends(get_db_session),
) -> TeamMembershipResponse:
    """Add an eligible assistant to a team."""

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )
    if not resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage team members",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_active_team(team)
    _require_unmanaged_team(team)

    created_workspace_coordinator = False
    created_workspace_coordinator_id: int | None = None
    refresh_payloads = []
    try:
        if body.member_user_id:
            if org_member_dao.get_member(body.member_user_id, organization_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organization member not found.",
                )
            assistant, created_workspace_coordinator, refresh_payloads = (
                await _add_coordinator_to_team(
                    session,
                    team=team,
                    member_user_id=body.member_user_id,
                    actor_user_id=user_id,
                )
            )
            if created_workspace_coordinator:
                created_workspace_coordinator_id = assistant.agent_id
            if not team_dao.is_team_member(team_id, body.member_user_id):
                team_dao.add_member(team_id, body.member_user_id)
            response.status_code = status.HTTP_201_CREATED
        else:
            assistant = team_dao.get_assistant(body.assistant_id)
            if assistant is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Assistant not found.",
                )
            _require_assistant_membership_target_allowed(
                actor_user_id=user_id,
                team=team,
                assistant=assistant,
            )
            existing_membership = team_dao.get_assistant_membership(
                team_id=team.id,
                assistant_id=assistant.agent_id,
            )
            result = add_assistant_to_team(
                session,
                team=team,
                assistant=assistant,
                actor_user_id=user_id,
            )
            if existing_membership:
                response.status_code = status.HTTP_200_OK
            else:
                refresh_payloads = result.refresh_payloads
        session.commit()
    except HTTPException:
        session.rollback()
        if (
            created_workspace_coordinator
            and created_workspace_coordinator_id is not None
        ):
            await delete_pubsub_topic(str(created_workspace_coordinator_id))
        raise
    except Exception:
        session.rollback()
        if (
            created_workspace_coordinator
            and created_workspace_coordinator_id is not None
        ):
            await delete_pubsub_topic(str(created_workspace_coordinator_id))
        raise

    await publish_membership_refreshes_best_effort(refresh_payloads)
    return TeamMembershipResponse(
        membership_status=TeamMembershipStatus.active,
        assistant_id=assistant.agent_id,
        team_id=team.id,
    )


@router.delete(
    "/organizations/{organization_id}/teams/{team_id}/assistant-members/{assistant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_team_assistant_member(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> Response:
    """Remove an assistant from a team."""

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )
    if not resource_access_dao.check_org_member_permission(
        user_id,
        organization_id,
        "org:write",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage team members",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )
    _require_active_team(team)

    membership = team_dao.get_assistant_membership(
        team_id=team_id,
        assistant_id=assistant_id,
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team assistant membership not found.",
        )

    assistant = team_dao.get_assistant(assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    await purge_team_member_overlay(
        session,
        assistant_id=assistant_id,
        team_id=team_id,
    )
    refresh_payloads = membership_refresh_payloads(session, [assistant])
    session.commit()
    await publish_membership_refreshes_best_effort(refresh_payloads)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/organizations/{organization_id}/teams/{team_id}/assistant-members",
    response_model=List[TeamAssistantMember],
    status_code=status.HTTP_200_OK,
)
def list_team_assistant_members(
    request_fastapi: Request,
    organization_id: int,
    team_id: int,
    session: Session = Depends(get_db_session),
) -> List[TeamAssistantMember]:
    """List assistant members for a team."""

    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    is_owner = org.owner_id == user_id
    is_member = org_member_dao.filter(user_id=user_id, organization_id=organization_id)
    if not is_owner and not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of this organization to view teams",
        )

    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    return [
        TeamAssistantMember(
            assistant_id=assistant.agent_id,
            team_id=membership.team_id,
            user_id=assistant.user_id,
            organization_id=assistant.organization_id,
            added_by=membership.added_by,
            created_at=membership.created_at,
        )
        for membership, assistant in team_dao.list_assistant_members(team_id)
    ]


@router.get(
    "/assistants/{assistant_id}/teams",
    response_model=List[TeamSummary],
    status_code=status.HTTP_200_OK,
)
def list_teams_for_assistant(
    request_fastapi: Request,
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> List[TeamSummary]:
    """List teams where an assistant is a live member."""

    user_id = request_fastapi.state.user_id
    team_dao = TeamDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    assistant = team_dao.get_assistant(assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    if assistant.user_id == user_id:
        pass
    elif assistant.is_coordinator and assistant.organization_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this assistant.",
        )
    elif (
        assistant.organization_id is not None
        and resource_access_dao.check_org_member_permission(
            user_id,
            assistant.organization_id,
            "org:read",
        )
    ):
        pass
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this assistant.",
        )

    return [
        TeamSummary(
            team_id=team.id,
            name=team.name,
            description=team.description,
        )
        for team in team_dao.list_teams_for_assistant(assistant_id)
    ]


@router.post(
    "/resources/{resource_type}/{resource_id}/access",
    response_model=ResourceAccessResponse,
    status_code=status.HTTP_201_CREATED,
)
def grant_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_data: ResourceAccessGrant,
    session: Session = Depends(get_db_session),
) -> ResourceAccessResponse:
    """
    Grant access to a resource (project).

    Only works for organizational resources. Personal resources cannot be shared.
    User must have appropriate permissions on the resource.

    Note: For org-level permissions, use OrganizationMember roles instead.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource ("project").
    :param resource_id: Resource ID.
    :param access_data: Access grant data.
    :param session: Database session.
    :return: Created access entry.
    """
    # Validate resource type - only "project" is supported for ResourceAccess
    if resource_type != "project":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resource type: {resource_type}. Only 'project' is supported. "
            "For org-level permissions, manage OrganizationMember roles instead.",
        )

    user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)

    # Verify resource is not personal
    if resource_access_dao._is_personal_resource(resource_type, resource_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot share personal resources. Only organizational resources can be shared.",
        )

    # Check if user has write/owner permission on the resource
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        f"{resource_type}:write",
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to share this resource",
        )

    # Verify role exists
    role = role_dao.get(access_data.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {access_data.role_id} not found",
        )

    try:
        access = resource_access_dao.grant_access(
            resource_type=resource_type,
            resource_id=resource_id,
            role_id=access_data.role_id,
            grantee_type=access_data.grantee_type,
            grantee_id=access_data.grantee_id,
        )
        session.commit()

        return ResourceAccessResponse(
            id=access.id,
            resource_type=access.resource_type,
            resource_id=access.resource_id,
            role_id=access.role_id,
            role_name=role.name,
            grantee_type=access.grantee_type,
            grantee_id=access.grantee_id,
            created_at=access.created_at,
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to grant access: {str(e)}",
        )


@router.delete(
    "/resources/{resource_type}/{resource_id}/access",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_data: ResourceAccessRevoke,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Revoke access to a resource (project).

    User must have appropriate permissions on the resource.

    Note: For org-level permissions, use OrganizationMember roles instead.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource ("project").
    :param resource_id: Resource ID.
    :param access_data: Access revoke data.
    :param session: Database session.
    """
    # Validate resource type - only "project" is supported for ResourceAccess
    if resource_type != "project":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resource type: {resource_type}. Only 'project' is supported. "
            "For org-level permissions, manage OrganizationMember roles instead.",
        )

    user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)

    # Check if user has write/owner permission on the resource
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        f"{resource_type}:write",
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify access for this resource",
        )

    try:
        resource_access_dao.revoke_access(
            resource_type=resource_type,
            resource_id=resource_id,
            grantee_type=access_data.grantee_type,
            grantee_id=access_data.grantee_id,
            role_id=access_data.role_id,
        )
        session.commit()
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke access: {str(e)}",
        )


@router.patch(
    "/resources/{resource_type}/{resource_id}/access/{access_id}",
    response_model=ResourceAccessResponse,
    status_code=status.HTTP_200_OK,
)
def update_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_id: int,
    update_data: ResourceAccessUpdate,
    session: Session = Depends(get_db_session),
) -> ResourceAccessResponse:
    """
    Update an existing resource access grant (change role).

    This is a more atomic alternative to revoking and re-granting access.
    Preserves the access ID and created_at timestamp.

    User must have write permission on the resource.

    Note: For org-level permissions, use OrganizationMember roles instead.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource ("project").
    :param resource_id: Resource ID.
    :param access_id: ResourceAccess ID to update.
    :param update_data: Update data containing new role_id.
    :param session: Database session.
    :return: Updated access entry.
    """
    # Validate resource type - only "project" is supported for ResourceAccess
    if resource_type != "project":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resource type: {resource_type}. Only 'project' is supported. "
            "For org-level permissions, manage OrganizationMember roles instead.",
        )

    user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)

    # Verify the access entry exists and belongs to this resource
    existing_access = resource_access_dao.get(access_id)
    if not existing_access:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Access grant with id {access_id} not found",
        )

    if (
        existing_access.resource_type != resource_type
        or existing_access.resource_id != resource_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Access grant {access_id} does not belong to this resource",
        )

    # Check if user has write permission on the resource
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        f"{resource_type}:write",
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify access for this resource",
        )

    # Verify the new role exists
    new_role = role_dao.get(update_data.role_id)
    if not new_role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {update_data.role_id} not found",
        )

    try:
        # Update the role
        updated_access = resource_access_dao.update_role(
            access_id=access_id,
            new_role_id=update_data.role_id,
        )
        session.commit()

        return ResourceAccessResponse(
            id=updated_access.id,
            resource_type=updated_access.resource_type,
            resource_id=updated_access.resource_id,
            role_id=updated_access.role_id,
            role_name=new_role.name,
            grantee_type=updated_access.grantee_type,
            grantee_id=updated_access.grantee_id,
            created_at=updated_access.created_at,
        )
    except ValueError as e:
        # Unique constraint violation
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update access: {str(e)}",
        )


@router.get(
    "/resources/{resource_type}/{resource_id}/access",
    response_model=ResourceAccessListResponse,
    status_code=status.HTTP_200_OK,
)
def list_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    session: Session = Depends(get_db_session),
) -> ResourceAccessListResponse:
    """
    List all access entries for a resource (project).

    User must have read permission on the resource.

    Note: For org-level permissions, use the /organizations/{id}/members endpoint instead.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource ("project").
    :param resource_id: Resource ID.
    :param session: Database session.
    :return: List of access entries.
    """
    # Validate resource type - only "project" is supported for ResourceAccess
    if resource_type != "project":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resource type: {resource_type}. Only 'project' is supported. "
            "For org-level permissions, use the /organizations/{id}/members endpoint.",
        )

    user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)
    user_dao = UserDAO(session)
    team_dao = TeamDAO(session)

    # Check if user has read permission on the resource
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        resource_type,
        resource_id,
        f"{resource_type}:read",
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view access for this resource",
        )

    access_entries = resource_access_dao.get_resource_access(resource_type, resource_id)

    response_entries = []
    for entry in access_entries:
        role = role_dao.get(entry.role_id)
        role_name = role.name if role else "Unknown"

        # Get grantee name
        grantee_name = None
        if entry.grantee_type == "user":
            user = user_dao.get_by_id(entry.grantee_id)
            grantee_name = user[0].email if user else entry.grantee_id
        elif entry.grantee_type == "team":
            try:
                team = team_dao.get(int(entry.grantee_id))
                grantee_name = team.name if team else entry.grantee_id
            except ValueError:
                grantee_name = entry.grantee_id

        response_entries.append(
            ResourceAccessResponse(
                id=entry.id,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                role_id=entry.role_id,
                role_name=role_name,
                grantee_type=entry.grantee_type,
                grantee_id=entry.grantee_id,
                grantee_name=grantee_name,
                created_at=entry.created_at,
            ),
        )

    return ResourceAccessListResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        access_entries=response_entries,
    )


@router.get(
    "/resources/{resource_type}/{resource_id}/access/user/{user_id}",
    response_model=UserResourceAccessResponse,
    status_code=status.HTTP_200_OK,
)
def get_user_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    user_id: str,
    session: Session = Depends(get_db_session),
) -> UserResourceAccessResponse:
    """
    Get a specific user's access entries for a resource.

    Returns all access entries (direct user grants + team-based grants)
    and the effective role (highest permission level).

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource ("project").
    :param resource_id: Resource ID.
    :param user_id: User ID to check access for.
    :param session: Database session.
    :return: User's access entries and effective role.
    """
    # Validate resource type - only "project" is supported
    if resource_type != "project":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid resource type: {resource_type}. Only 'project' is supported.",
        )

    requesting_user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)
    team_dao = TeamDAO(session)

    # Check if requesting user has read permission on the resource
    has_permission = resource_access_dao.check_user_permission(
        requesting_user_id,
        resource_type,
        resource_id,
        f"{resource_type}:read",
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view access for this resource",
        )

    # Get user's access entries (direct + team-based)
    access_entries = resource_access_dao.get_user_access(
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )

    # Build response entries
    response_entries = []
    role_priority = {"Owner": 4, "Admin": 3, "Member": 2, "Viewer": 1}
    highest_role = None
    highest_priority = 0

    for entry in access_entries:
        role = role_dao.get(entry.role_id)
        role_name = role.name if role else "Unknown"

        # Get permissions for this role
        role_permissions = role_dao.get_role_permissions(entry.role_id)
        permission_names = [p.name for p in role_permissions]

        # Determine if this is a direct grant or team-based
        is_team_grant = entry.grantee_type == "team"
        team_id = None
        team_name = None

        if is_team_grant:
            try:
                team_id = int(entry.grantee_id)
                team = team_dao.get(team_id)
                team_name = team.name if team else None
            except ValueError:
                pass

        response_entries.append(
            UserResourceAccessEntry(
                id=entry.id,
                role_id=entry.role_id,
                role_name=role_name,
                permissions=permission_names,
                grantee_type=entry.grantee_type,
                source="team" if is_team_grant else "direct",
                team_id=team_id,
                team_name=team_name,
                created_at=entry.created_at,
            ),
        )

        # Track highest permission
        priority = role_priority.get(role_name, 0)
        if priority > highest_priority:
            highest_priority = priority
            highest_role = role_name

    return UserResourceAccessResponse(
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        access_entries=response_entries,
        effective_role=highest_role,
    )
