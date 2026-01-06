"""Team management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

# Async DAOs
from orchestra.db.dao.async_auth_user_dao import AsyncAuthUserDAO
from orchestra.db.dao.async_organization_dao import AsyncOrganizationDAO
from orchestra.db.dao.async_organization_member_dao import AsyncOrganizationMemberDAO
from orchestra.db.dao.async_resource_access_dao import AsyncResourceAccessDAO
from orchestra.db.dao.async_role_dao import AsyncRoleDAO
from orchestra.db.dao.async_team_dao import AsyncTeamDAO
from orchestra.db.dependencies import get_async_db_session
from orchestra.web.api.teams.schema import (
    ResourceAccessGrant,
    ResourceAccessListResponse,
    ResourceAccessResponse,
    ResourceAccessRevoke,
    ResourceAccessUpdate,
    TeamCreate,
    TeamMemberAdd,
    TeamResponse,
    TeamUpdate,
    TeamWithMembersResponse,
    UserResourceAccessEntry,
    UserResourceAccessResponse,
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
    session: AsyncSession = Depends(get_async_db_session),
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
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    resource_access_dao = AsyncResourceAccessDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
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

    try:
        team = await team_dao.create(
            name=team_data.name,
            organization_id=organization_id,
            description=team_data.description,
        )
        await session.commit()

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
    session: AsyncSession = Depends(get_async_db_session),
) -> List[TeamResponse]:
    """
    List all teams in an organization.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param session: Database session.
    :return: List of teams.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    org_member_dao = AsyncOrganizationMemberDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is a member
    is_owner = org.owner_id == user_id
    is_member = await org_member_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )

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
    session: AsyncSession = Depends(get_async_db_session),
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
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    org_member_dao = AsyncOrganizationMemberDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Get team
    team = await team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    # Check if user is a member
    is_owner = org.owner_id == user_id
    is_member = await org_member_dao.filter(
        user_id=user_id,
        organization_id=organization_id,
    )

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
    session: AsyncSession = Depends(get_async_db_session),
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
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    resource_access_dao = AsyncResourceAccessDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
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

    # Get team
    team = await team_dao.get(team_id)
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
        await team_dao.update(
            id=team_id,
            name=team_data.name,
            description=team_data.description,
        )
        await session.commit()

        # Refresh team
        team = await team_dao.get(team_id)

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
    session: AsyncSession = Depends(get_async_db_session),
) -> None:
    """
    Delete a team.

    Requires org:write permission (Owner and Admin roles have this).

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param team_id: Team ID.
    :param session: Database session.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    resource_access_dao = AsyncResourceAccessDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
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

    # Get team
    team = await team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        await team_dao.delete(team_id)
        await session.commit()
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
    session: AsyncSession = Depends(get_async_db_session),
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
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    org_member_dao = AsyncOrganizationMemberDAO(session)
    auth_user_dao = AsyncAuthUserDAO(session)
    resource_access_dao = AsyncResourceAccessDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
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

    # Get team
    team = await team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        # Verify all users exist and are org members
        for user_id_to_add in member_data.user_ids:
            user = await auth_user_dao.get_by_id(user_id_to_add)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User with id {user_id_to_add} not found",
                )

            # Check if user is org member
            is_owner = org.owner_id == user_id_to_add
            is_member = await org_member_dao.filter(
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
                await team_dao.add_member(team_id, user_id_to_add)

        await session.commit()

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
    session: AsyncSession = Depends(get_async_db_session),
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
    org_dao = AsyncOrganizationDAO(session)
    team_dao = AsyncTeamDAO(session)
    resource_access_dao = AsyncResourceAccessDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
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

    # Get team
    team = await team_dao.get(team_id)
    if not team or team.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team with id {team_id} not found in this organization",
        )

    try:
        await team_dao.remove_member(team_id, user_id_to_remove)
        await session.commit()

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
    session: AsyncSession = Depends(get_async_db_session),
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
    resource_access_dao = AsyncResourceAccessDAO(session)
    role_dao = AsyncRoleDAO(session)

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
    role = await role_dao.get(access_data.role_id)
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
        await session.commit()

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
    session: AsyncSession = Depends(get_async_db_session),
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
    resource_access_dao = AsyncResourceAccessDAO(session)

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
        await session.commit()
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
async def update_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    access_id: int,
    update_data: ResourceAccessUpdate,
    session: AsyncSession = Depends(get_async_db_session),
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
    resource_access_dao = AsyncResourceAccessDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify the access entry exists and belongs to this resource
    existing_access = await resource_access_dao.get(access_id)
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
    new_role = await role_dao.get(update_data.role_id)
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
        await session.commit()

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
async def list_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    session: AsyncSession = Depends(get_async_db_session),
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
    resource_access_dao = AsyncResourceAccessDAO(session)
    role_dao = AsyncRoleDAO(session)
    auth_user_dao = AsyncAuthUserDAO(session)
    team_dao = AsyncTeamDAO(session)

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
        role = await role_dao.get(entry.role_id)
        role_name = role.name if role else "Unknown"

        # Get grantee name
        grantee_name = None
        if entry.grantee_type == "user":
            user = await auth_user_dao.get_by_id(entry.grantee_id)
            grantee_name = user[0].email if user else entry.grantee_id
        elif entry.grantee_type == "team":
            try:
                team = await team_dao.get(int(entry.grantee_id))
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
async def get_user_resource_access(
    request_fastapi: Request,
    resource_type: str,
    resource_id: int,
    user_id: str,
    session: AsyncSession = Depends(get_async_db_session),
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
    resource_access_dao = AsyncResourceAccessDAO(session)
    role_dao = AsyncRoleDAO(session)
    team_dao = AsyncTeamDAO(session)

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
        role = await role_dao.get(entry.role_id)
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
                team = await team_dao.get(team_id)
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
