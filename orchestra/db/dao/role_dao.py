"""Data Access Object for Role model."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Permission, Role, RolePermission


class RoleDAO:
    """DAO for managing roles."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        name: str,
        description: Optional[str] = None,
        organization_id: Optional[int] = None,
        is_system_role: bool = False,
    ) -> Role:
        """
        Create a new role.

        :param name: Role name.
        :param description: Role description.
        :param organization_id: Organization ID (NULL for system roles).
        :param is_system_role: Whether this is a system role.
        :return: The created Role object.
        """
        role = Role(
            name=name,
            description=description,
            organization_id=organization_id,
            is_system_role=is_system_role,
        )
        self.session.add(role)
        self.session.flush()
        return role

    def get(self, id: int) -> Optional[Role]:
        """
        Get a role by ID.

        :param id: Role ID.
        :return: Role object or None if not found.
        """
        return self.session.query(Role).filter_by(id=id).first()

    def get_by_name(
        self,
        name: str,
        organization_id: Optional[int] = None,
    ) -> Optional[Role]:
        """
        Get a role by name and organization.

        :param name: Role name.
        :param organization_id: Organization ID (None for system roles).
        :return: Role object or None if not found.
        """
        query = self.session.query(Role).filter(Role.name == name)
        if organization_id is None:
            query = query.filter(Role.organization_id.is_(None))
        else:
            query = query.filter(Role.organization_id == organization_id)
        return query.first()

    def filter(
        self,
        id: Optional[int] = None,
        name: Optional[str] = None,
        organization_id: Optional[int] = None,
        is_system_role: Optional[bool] = None,
    ) -> List[Role]:
        """
        Filter roles by various criteria.

        :param id: Role ID.
        :param name: Role name.
        :param organization_id: Organization ID (use None to filter system roles).
        :param is_system_role: Whether to filter for system roles.
        :return: List of matching roles.
        """
        query = select(Role)
        if id:
            query = query.where(Role.id == id)
        if name:
            query = query.where(Role.name == name)
        if organization_id is not None:
            query = query.where(Role.organization_id == organization_id)
        if is_system_role is not None:
            query = query.where(Role.is_system_role == is_system_role)
        rows = self.session.execute(query)
        return [row[0] for row in rows.fetchall()]

    def get_system_roles(self) -> List[Role]:
        """
        Get all system roles (Owner, Admin, Member, Viewer).

        :return: List of system roles.
        """
        return list(
            self.session.query(Role)
            .filter(Role.is_system_role == True)  # noqa: E712
            .all(),
        )

    def get_organization_roles(self, organization_id: int) -> List[Role]:
        """
        Get all roles for an organization (including system roles).

        :param organization_id: Organization ID.
        :return: List of roles available to the organization.
        """
        # Get system roles (available to all orgs)
        system_roles = self.get_system_roles()

        # Get org-specific roles
        org_roles = list(
            self.session.query(Role)
            .filter(Role.organization_id == organization_id)
            .all(),
        )

        return system_roles + org_roles

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        """
        Update a role (only for custom roles, not system roles).

        :param id: Role ID.
        :param name: New role name.
        :param description: New role description.
        """
        role = self.get(id)
        if role and not role.is_system_role:
            if name:
                role.name = name
            if description:
                role.description = description
            self.session.flush()

    def delete(self, id: int) -> None:
        """
        Delete a role (only for custom roles, not system roles).

        :param id: Role ID.
        :raises ValueError: If trying to delete a system role.
        """
        role = self.get(id)
        if role:
            if role.is_system_role:
                raise ValueError("Cannot delete system roles")
            self.session.delete(role)
            self.session.flush()

    def add_permission(self, role_id: int, permission_id: int) -> None:
        """
        Add a permission to a role.

        :param role_id: Role ID.
        :param permission_id: Permission ID.
        """
        # Check if already exists
        existing = (
            self.session.query(RolePermission)
            .filter(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
            .first()
        )

        if not existing:
            role_permission = RolePermission(
                role_id=role_id,
                permission_id=permission_id,
            )
            self.session.add(role_permission)
            self.session.flush()

    def remove_permission(self, role_id: int, permission_id: int) -> None:
        """
        Remove a permission from a role.

        :param role_id: Role ID.
        :param permission_id: Permission ID.
        """
        role_permission = (
            self.session.query(RolePermission)
            .filter(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
            .first()
        )

        if role_permission:
            self.session.delete(role_permission)
            self.session.flush()

    def get_role_permissions(self, role_id: int) -> List[Permission]:
        """
        Get all permissions for a role.

        :param role_id: Role ID.
        :return: List of permissions.
        """
        return list(
            self.session.query(Permission)
            .join(RolePermission)
            .filter(RolePermission.role_id == role_id)
            .all(),
        )

    def resolve_role_id(
        self,
        role_id: Optional[int] = None,
        role_name: Optional[str] = None,
    ) -> Optional[int]:
        """
        Resolve a role ID from either a direct ID or a system role name.

        :param role_id: Explicit role ID (takes precedence if provided).
        :param role_name: System role name to look up (used if role_id is None).
        :return: The resolved role ID, or None if neither param is provided.
        :raises ValueError: If role_name is given but no matching system role exists.
        """
        if role_id is not None:
            return role_id
        if role_name:
            role = self.get_by_name(role_name, organization_id=None)
            if not role:
                raise ValueError(f"Role '{role_name}' not found")
            return role.id
        return None

    def has_permission(
        self,
        role_id: int,
        permission_name: str,
    ) -> bool:
        """
        Check if a role has a specific permission.

        :param role_id: Role ID.
        :param permission_name: Permission name (e.g., 'project:read').
        :return: True if role has the permission, False otherwise.
        """
        count = (
            self.session.query(RolePermission)
            .join(Permission)
            .filter(
                RolePermission.role_id == role_id,
                Permission.name == permission_name,
            )
            .count()
        )
        return count > 0
