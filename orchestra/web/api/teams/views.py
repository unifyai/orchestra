"""Team management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.teams.schema import (
    ResourceAccessGrant,
    ResourceAccessListResponse,
    ResourceAccessResponse,
    ResourceAccessRevoke,
    TeamCreate,
    TeamMemberAdd,
    TeamResponse,
    TeamUpdate,
    TeamWithMembersResponse,
)

router = APIRouter()


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

    Only organization owners can create teams.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_data: Team creation data.
    :param session: Database session.
    :return: Created team.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can create teams",
        )

    # Check for duplicate team name
    existing_team = team_dao.get_by_name(team_data.name, organization_id)
    if existing_team:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Team with name '{team_data.name}' already exists in this organization",
        )

    try:
        team = team_dao.create(
            name=team_data.name,
            organization_id=organization_id,
            description=team_data.description,
        )
        session.commit()

        return TeamResponse(
            id=team.id,
            name=team.name,
            description=team.description,
            organization_id=team.organization_id,
            created_at=team.created_at,
            member_count=0,
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create team: {str(e)}",
        )


@router.get(
    "/organizations/{organization_id}/teams",
    response_model=List[TeamResponse],
    status_code=status.HTTP_200_OK,
)
async def list_teams(
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
            member_count=len(team_dao.get_team_members(team.id)),
        )
        for team in teams
    ]


@router.get(
    "/organizations/{organization_id}/teams/{team_id}",
    response_model=TeamWithMembersResponse,
    status_code=status.HTTP_200_OK,
)
async def get_team(
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

    Only organization owners can update teams.

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

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can update teams",
        )

    # Get team
    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    # Check for duplicate name if updating name
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
        session.commit()

        # Refresh team
        team = team_dao.get(team_id)

        return TeamResponse(
            id=team.id,
            name=team.name,
            description=team.description,
            organization_id=team.organization_id,
            created_at=team.created_at,
            member_count=len(team_dao.get_team_members(team_id)),
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update team: {str(e)}",
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
) -> None:
    """
    Delete a team.

    Only organization owners can delete teams.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param session: Database session.
    """
    user_id = request_fastapi.state.user_id
    org_dao = OrganizationDAO(session)
    team_dao = TeamDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can delete teams",
        )

    # Get team
    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        team_dao.delete(team_id)
        session.commit()
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete team: {str(e)}",
        )


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

    Only organization owners can add members to teams.

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
    auth_user_dao = AuthUserDAO(session)

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can add members to teams",
        )

    # Get team
    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        # Verify all users exist and are org members
        for user_id_to_add in member_data.user_ids:
            user = auth_user_dao.get_by_id(user_id_to_add)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User with id {user_id_to_add} not found",
                )

            # Check if user is org member
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

            # Add to team (skip if already member)
            if not team_dao.is_team_member(team_id, user_id_to_add):
                team_dao.add_member(team_id, user_id_to_add)

        session.commit()

        members = team_dao.get_team_members(team_id)

        return TeamWithMembersResponse(
            id=team.id,
            name=team.name,
            description=team.description,
            organization_id=team.organization_id,
            created_at=team.created_at,
            members=members,
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add team members: {str(e)}",
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

    Only organization owners can remove members from teams.

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

    # Verify organization exists
    org = org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can remove members from teams",
        )

    # Get team
    team = team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        team_dao.remove_member(team_id, user_id_to_remove)
        session.commit()

        members = team_dao.get_team_members(team_id)

        return TeamWithMembersResponse(
            id=team.id,
            name=team.name,
            description=team.description,
            organization_id=team.organization_id,
            created_at=team.created_at,
            members=members,
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove team member: {str(e)}",
        )


@router.post(
    "/resources/{resource_type}/{resource_id}/access",
    response_model=ResourceAccessResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_data: ResourceAccessGrant,
    session: Session = Depends(get_db_session),
) -> ResourceAccessResponse:
    """
    Grant access to a resource (project/interface/tab/tile).

    Only works for organizational resources. Personal resources cannot be shared.
    User must have appropriate permissions on the resource.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource.
    :param resource_id: Resource ID.
    :param access_data: Access grant data.
    :param session: Database session.
    :return: Created access entry.
    """
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
async def revoke_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_data: ResourceAccessRevoke,
    session: Session = Depends(get_db_session),
) -> None:
    """
    Revoke access to a resource.

    User must have appropriate permissions on the resource.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource.
    :param resource_id: Resource ID.
    :param access_data: Access revoke data.
    :param session: Database session.
    """
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


@router.get(
    "/resources/{resource_type}/{resource_id}/access",
    response_model=ResourceAccessListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    session: Session = Depends(get_db_session),
) -> ResourceAccessListResponse:
    """
    List all access entries for a resource.

    User must have read permission on the resource.

    :param request_fastapi: FastAPI request object.
    :param resource_type: Type of resource.
    :param resource_id: Resource ID.
    :param session: Database session.
    :return: List of access entries.
    """
    user_id = request_fastapi.state.user_id
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)
    auth_user_dao = AuthUserDAO(session)
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
            user = auth_user_dao.get_by_id(entry.grantee_id)
            grantee_name = user.email if user else entry.grantee_id
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
