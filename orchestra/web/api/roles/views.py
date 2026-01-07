"""Role and permission management endpoints."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

# Async DAOs
from orchestra.db.dao.async_organization_dao import AsyncOrganizationDAO
from orchestra.db.dao.async_permission_dao import AsyncPermissionDAO
from orchestra.db.dao.async_role_dao import AsyncRoleDAO
from orchestra.db.dependencies import get_async_db_session
from orchestra.web.api.roles.schema import (
    PermissionResponse,
    RoleCreate,
    RolePermissionAdd,
    RoleResponse,
    RoleUpdate,
)

router = APIRouter()


@router.get(
    "/permissions",
    response_model=List[PermissionResponse],
    status_code=status.HTTP_200_OK,
)
async def list_permissions(
    resource_type: str | None = None,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[PermissionResponse]:
    """
    List all available permissions, optionally filtered by resource type.

    :param resource_type: Optional filter by resource type (e.g., 'project', 'interface').
    :param session: Database session.
    :return: List of permissions.
    """
    permission_dao = AsyncPermissionDAO(session)

    if resource_type:
        permissions = permission_dao.get_by_resource_type(resource_type)
    else:
        permissions = await permission_dao.list_all()

    return [
        PermissionResponse(
            id=perm.id,
            name=perm.name,
            description=perm.description,
            resource_type=perm.resource_type,
            action=perm.action,
            created_at=perm.created_at,
        )
        for perm in permissions
    ]


@router.get(
    "/organizations/{organization_id}/roles",
    response_model=List[RoleResponse],
    status_code=status.HTTP_200_OK,
)
async def list_organization_roles(
    request_fastapi: Request,
    organization_id: int,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[RoleResponse]:
    """
    List all roles available to an organization (system roles + custom roles).

    Only organization members can view roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param session: Database session.
    :return: List of roles.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Get roles for the organization
    roles = role_dao.get_organization_roles(organization_id)

    return [_role_to_response(role, role_dao) for role in roles]


@router.post(
    "/organizations/{organization_id}/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_role(
    request_fastapi: Request,
    organization_id: int,
    role_data: RoleCreate,
    session: AsyncSession = Depends(get_async_db_session),
) -> RoleResponse:
    """
    Create a custom role for an organization.

    Only organization owners can create custom roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_data: Role creation data.
    :param session: Database session.
    :return: Created role.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can create custom roles",
        )

    # Check if role name already exists for this organization
    existing_role = role_dao.get_by_name(role_data.name, organization_id)
    if existing_role:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Role '{role_data.name}' already exists in this organization",
        )

    try:
        # Create the role
        role = await role_dao.create(
            name=role_data.name,
            description=role_data.description,
            organization_id=organization_id,
            is_system_role=False,
        )

        # Add permissions to the role
        for permission_id in role_data.permission_ids:
            role_dao.add_permission(role.id, permission_id)

        await session.commit()

        return _role_to_response(role, role_dao)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create role: {str(e)}",
        )


@router.get(
    "/organizations/{organization_id}/roles/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def get_role(
    request_fastapi: Request,
    organization_id: int,
    role_id: int,
    session: AsyncSession = Depends(get_async_db_session),
) -> RoleResponse:
    """
    Get details of a specific role.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_id: Role ID.
    :param session: Database session.
    :return: Role details.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Get role
    role = await role_dao.get(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found",
        )

    # Verify role belongs to the organization (or is a system role)
    if role.organization_id is not None and role.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found in this organization",
        )

    return _role_to_response(role, role_dao)


@router.patch(
    "/organizations/{organization_id}/roles/{role_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def update_role(
    request_fastapi: Request,
    organization_id: int,
    role_id: int,
    role_data: RoleUpdate,
    session: AsyncSession = Depends(get_async_db_session),
) -> RoleResponse:
    """
    Update a custom role (name and description only).

    Only organization owners can update custom roles.
    Cannot update system roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_id: Role ID.
    :param role_data: Role update data.
    :param session: Database session.
    :return: Updated role.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can update roles",
        )

    # Get role
    role = await role_dao.get(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found",
        )

    # Cannot update system roles (check before organization validation)
    if role.is_system_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot update system roles",
        )

    # Verify role belongs to the organization
    if role.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found in this organization",
        )

    try:
        await role_dao.update(
            id=role_id,
            name=role_data.name,
            description=role_data.description,
        )
        await session.commit()

        return _role_to_response(role, role_dao)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update role: {str(e)}",
        )


@router.delete(
    "/organizations/{organization_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_role(
    request_fastapi: Request,
    organization_id: int,
    role_id: int,
    session: AsyncSession = Depends(get_async_db_session),
) -> None:
    """
    Delete a custom role.

    Only organization owners can delete custom roles.
    Cannot delete system roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_id: Role ID.
    :param session: Database session.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can delete roles",
        )

    # Get role
    role = await role_dao.get(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found",
        )

    # Cannot delete system roles (check before organization validation)
    if role.is_system_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete system roles",
        )

    # Verify role belongs to the organization
    if role.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found in this organization",
        )

    try:
        await role_dao.delete(role_id)
        await session.commit()
        return None
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete role: {str(e)}",
        )


@router.post(
    "/organizations/{organization_id}/roles/{role_id}/permissions",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def add_permissions_to_role(
    request_fastapi: Request,
    organization_id: int,
    role_id: int,
    permission_data: RolePermissionAdd,
    session: AsyncSession = Depends(get_async_db_session),
) -> RoleResponse:
    """
    Add permissions to a custom role.

    Only organization owners can modify role permissions.
    Cannot modify system roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_id: Role ID.
    :param permission_data: Permissions to add.
    :param session: Database session.
    :return: Updated role.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can modify role permissions",
        )

    # Get role
    role = await role_dao.get(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found",
        )

    # Cannot modify system roles (check before organization validation)
    if role.is_system_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify system role permissions",
        )

    # Verify role belongs to the organization
    if role.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found in this organization",
        )

    try:
        for permission_id in permission_data.permission_ids:
            role_dao.add_permission(role_id, permission_id)

        await session.commit()

        return _role_to_response(role, role_dao)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add permissions: {str(e)}",
        )


@router.delete(
    "/organizations/{organization_id}/roles/{role_id}/permissions/{permission_id}",
    response_model=RoleResponse,
    status_code=status.HTTP_200_OK,
)
async def remove_permission_from_role(
    request_fastapi: Request,
    organization_id: int,
    role_id: int,
    permission_id: int,
    session: AsyncSession = Depends(get_async_db_session),
) -> RoleResponse:
    """
    Remove a permission from a custom role.

    Only organization owners can modify role permissions.
    Cannot modify system roles.

    :param request_fastapi: FastAPI request object.
    :param organization_id: Organization ID.
    :param role_id: Role ID.
    :param permission_id: Permission ID to remove.
    :param session: Database session.
    :return: Updated role.
    """
    user_id = request_fastapi.state.user_id
    org_dao = AsyncOrganizationDAO(session)
    role_dao = AsyncRoleDAO(session)

    # Verify organization exists
    org = await org_dao.get(organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {organization_id} not found",
        )

    # Check if user is the owner
    if org.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the organization owner can modify role permissions",
        )

    # Get role
    role = await role_dao.get(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found",
        )

    # Cannot modify system roles (check before organization validation)
    if role.is_system_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify system role permissions",
        )

    # Verify role belongs to the organization
    if role.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role with id {role_id} not found in this organization",
        )

    try:
        role_dao.remove_permission(role_id, permission_id)
        await session.commit()

        return _role_to_response(role, role_dao)
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove permission: {str(e)}",
        )


async def _role_to_response(role, role_dao: AsyncRoleDAO) -> RoleResponse:
    """Convert Role model to RoleResponse schema."""
    permissions = role_dao.get_role_permissions(role.id)

    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        organization_id=role.organization_id,
        is_system_role=role.is_system_role,
        created_at=role.created_at,
        permissions=[
            PermissionResponse(
                id=perm.id,
                name=perm.name,
                description=perm.description,
                resource_type=perm.resource_type,
                action=perm.action,
                created_at=perm.created_at,
            )
            for perm in permissions
        ],
    )
