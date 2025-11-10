"""Data Access Object for ResourceAccess model."""
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Interface,
    Project,
    ResourceAccess,
    Tab,
    TeamMember,
    Tile,
)


class ResourceAccessDAO:
    """DAO for managing resource access (RBAC)."""

    def __init__(self, session: Session):
        self.session = session

    def grant_access(
        self,
        resource_type: str,
        resource_id: int,
        role_id: int,
        grantee_type: str,
        grantee_id: str,
    ) -> ResourceAccess:
        """
        Grant access to a resource for a user or team.

        :param resource_type: Type of resource ('project', 'interface', etc.).
        :param resource_id: Resource ID.
        :param role_id: Role ID to grant.
        :param grantee_type: 'user' or 'team'.
        :param grantee_id: User ID or Team ID (as string).
        :return: The created ResourceAccess object.
        """
        access = ResourceAccess(
            resource_type=resource_type,
            resource_id=resource_id,
            role_id=role_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
        )
        self.session.add(access)
        self.session.flush()
        return access

    def revoke_access(
        self,
        resource_type: str,
        resource_id: int,
        grantee_type: str,
        grantee_id: str,
        role_id: Optional[int] = None,
    ) -> None:
        """
        Revoke access to a resource.

        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :param grantee_type: 'user' or 'team'.
        :param grantee_id: User ID or Team ID.
        :param role_id: Optional role ID to revoke specific role, or None to revoke all.
        """
        query = self.session.query(ResourceAccess).filter(
            ResourceAccess.resource_type == resource_type,
            ResourceAccess.resource_id == resource_id,
            ResourceAccess.grantee_type == grantee_type,
            ResourceAccess.grantee_id == grantee_id,
        )

        if role_id is not None:
            query = query.filter(ResourceAccess.role_id == role_id)

        query.delete()
        self.session.flush()

    def get_resource_access(
        self,
        resource_type: str,
        resource_id: int,
    ) -> List[ResourceAccess]:
        """
        Get all access entries for a resource.

        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :return: List of ResourceAccess objects.
        """
        return (
            self.session.query(ResourceAccess)
            .filter_by(resource_type=resource_type, resource_id=resource_id)
            .all()
        )

    def get_user_access(
        self,
        user_id: str,
        resource_type: str,
        resource_id: int,
    ) -> List[ResourceAccess]:
        """
        Get all access entries for a user on a specific resource (direct + team).

        :param user_id: User ID.
        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :return: List of ResourceAccess objects.
        """
        # Get teams user belongs to
        team_ids = (
            self.session.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .all()
        )
        team_id_strs = [str(team_id[0]) for team_id in team_ids]

        # Query for direct user access or team access
        return (
            self.session.query(ResourceAccess)
            .filter(
                ResourceAccess.resource_type == resource_type,
                ResourceAccess.resource_id == resource_id,
                or_(
                    and_(
                        ResourceAccess.grantee_type == "user",
                        ResourceAccess.grantee_id == user_id,
                    ),
                    and_(
                        ResourceAccess.grantee_type == "team",
                        ResourceAccess.grantee_id.in_(team_id_strs),
                    ),
                ),
            )
            .all()
        )

    def check_user_permission(
        self,
        user_id: str,
        resource_type: str,
        resource_id: int,
        permission_name: str,
    ) -> bool:
        """
        Check if a user has a specific permission on a resource.

        For personal projects: User is owner if project.user_id == user_id.
        For org projects: Check ResourceAccess + team memberships.

        :param user_id: User ID.
        :param resource_type: Type of resource ('project', 'interface', 'tab', 'tile').
        :param resource_id: Resource ID.
        :param permission_name: Permission name (e.g., 'project:read').
        :return: True if user has permission, False otherwise.
        """
        # Step 1: Check if this is a personal resource
        if self._is_personal_resource(resource_type, resource_id):
            # Personal resource: creator has all permissions
            return self._check_personal_ownership(resource_type, resource_id, user_id)

        # Step 2: Check org resource access via RBAC
        return self._check_org_permission(
            user_id,
            resource_type,
            resource_id,
            permission_name,
        )

    def _is_personal_resource(self, resource_type: str, resource_id: int) -> bool:
        """
        Check if a resource is personal (not associated with an organization).

        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :return: True if personal, False if organizational.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.organization_id is None

        if resource_type == "interface":
            interface = self.session.query(Interface).filter_by(id=resource_id).first()
            if interface:
                project = (
                    self.session.query(Project)
                    .filter_by(id=interface.project_id)
                    .first()
                )
                return project is not None and project.organization_id is None
            return False

        if resource_type == "tab":
            tab = self.session.query(Tab).filter_by(id=resource_id).first()
            if tab:
                interface = (
                    self.session.query(Interface).filter_by(id=tab.interface_id).first()
                )
                if interface:
                    project = (
                        self.session.query(Project)
                        .filter_by(id=interface.project_id)
                        .first()
                    )
                    return project is not None and project.organization_id is None
            return False

        if resource_type == "tile":
            tile = self.session.query(Tile).filter_by(id=resource_id).first()
            if tile:
                tab = self.session.query(Tab).filter_by(id=tile.tab_id).first()
                if tab:
                    interface = (
                        self.session.query(Interface)
                        .filter_by(id=tab.interface_id)
                        .first()
                    )
                    if interface:
                        project = (
                            self.session.query(Project)
                            .filter_by(id=interface.project_id)
                            .first()
                        )
                        return project is not None and project.organization_id is None
            return False

        return False

    def _check_personal_ownership(
        self,
        resource_type: str,
        resource_id: int,
        user_id: str,
    ) -> bool:
        """
        Check if user owns a personal resource.

        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :param user_id: User ID.
        :return: True if user is owner, False otherwise.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.user_id == user_id

        if resource_type == "interface":
            interface = self.session.query(Interface).filter_by(id=resource_id).first()
            if interface:
                project = (
                    self.session.query(Project)
                    .filter_by(id=interface.project_id)
                    .first()
                )
                return project is not None and project.user_id == user_id
            return False

        if resource_type == "tab":
            tab = self.session.query(Tab).filter_by(id=resource_id).first()
            if tab:
                interface = (
                    self.session.query(Interface).filter_by(id=tab.interface_id).first()
                )
                if interface:
                    project = (
                        self.session.query(Project)
                        .filter_by(id=interface.project_id)
                        .first()
                    )
                    return project is not None and project.user_id == user_id
            return False

        if resource_type == "tile":
            tile = self.session.query(Tile).filter_by(id=resource_id).first()
            if tile:
                tab = self.session.query(Tab).filter_by(id=tile.tab_id).first()
                if tab:
                    interface = (
                        self.session.query(Interface)
                        .filter_by(id=tab.interface_id)
                        .first()
                    )
                    if interface:
                        project = (
                            self.session.query(Project)
                            .filter_by(id=interface.project_id)
                            .first()
                        )
                        return project is not None and project.user_id == user_id
            return False

        return False

    def _check_org_permission(
        self,
        user_id: str,
        resource_type: str,
        resource_id: int,
        permission_name: str,
    ) -> bool:
        """
        Check org resource permission via RBAC.

        Logic:
        1. Check if resource has ANY explicit grants
        2. If YES: Only check explicit grants for this user (no implicit fallback)
        3. If NO: Apply implicit organization membership access

        :param user_id: User ID.
        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :param permission_name: Permission name.
        :return: True if user has permission, False otherwise.
        """
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.role_dao import RoleDAO

        role_dao = RoleDAO(self.session)

        # Step 1: Check if THIS RESOURCE has any explicit ResourceAccess entries
        any_resource_access = (
            self.session.query(ResourceAccess)
            .filter(
                ResourceAccess.resource_type == resource_type,
                ResourceAccess.resource_id == resource_id,
            )
            .first()
        )

        if any_resource_access:
            # Resource has explicit RBAC configured - only check explicit grants
            access_entries = self.get_user_access(user_id, resource_type, resource_id)

            if not access_entries:
                return False  # No explicit grant for this user

            # Check if any of the user's roles have the required permission
            role_ids = [access.role_id for access in access_entries]
            for role_id in role_ids:
                if role_dao.has_permission(role_id, permission_name):
                    return True

            return False  # Has grants but none provide this permission

        # Step 2: No explicit RBAC - use implicit organization membership
        org_id = self._get_resource_organization_id(resource_type, resource_id)

        if org_id is None:
            return False

        # Check if user is organization owner (implicit Owner role)
        from orchestra.db.dao.organization_dao import OrganizationDAO

        org_dao = OrganizationDAO(self.session)
        org = org_dao.get(org_id)

        if org and org.owner_id == user_id:
            owner_role = role_dao.get_by_name("Owner", organization_id=None)
            if owner_role and role_dao.has_permission(owner_role.id, permission_name):
                return True

        # Check if user is organization member (implicit Member role)
        org_member_dao = OrganizationMemberDAO(self.session)
        member = org_member_dao.filter(user_id=user_id, organization_id=org_id)

        if member:
            member_role = role_dao.get_by_name("Member", organization_id=None)
            if member_role and role_dao.has_permission(member_role.id, permission_name):
                return True

        return False

    def _get_resource_organization_id(
        self,
        resource_type: str,
        resource_id: int,
    ) -> Optional[int]:
        """
        Get the organization ID for a resource by traversing the hierarchy.

        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :return: Organization ID or None if personal/not found.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project.organization_id if project else None

        if resource_type == "interface":
            interface = self.session.query(Interface).filter_by(id=resource_id).first()
            if interface:
                project = (
                    self.session.query(Project)
                    .filter_by(id=interface.project_id)
                    .first()
                )
                return project.organization_id if project else None
            return None

        if resource_type == "tab":
            tab = self.session.query(Tab).filter_by(id=resource_id).first()
            if tab:
                interface = (
                    self.session.query(Interface).filter_by(id=tab.interface_id).first()
                )
                if interface:
                    project = (
                        self.session.query(Project)
                        .filter_by(id=interface.project_id)
                        .first()
                    )
                    return project.organization_id if project else None
            return None

        if resource_type == "tile":
            tile = self.session.query(Tile).filter_by(id=resource_id).first()
            if tile:
                tab = self.session.query(Tab).filter_by(id=tile.tab_id).first()
                if tab:
                    interface = (
                        self.session.query(Interface)
                        .filter_by(id=tab.interface_id)
                        .first()
                    )
                    if interface:
                        project = (
                            self.session.query(Project)
                            .filter_by(id=interface.project_id)
                            .first()
                        )
                        return project.organization_id if project else None
            return None

        return None

    def filter_accessible_resources(
        self,
        user_id: str,
        resource_type: str,
        permission_name: str,
    ) -> List[int]:
        """
        Get IDs of all resources of a given type that user can access.

        Returns both personal and organizational resource IDs.

        :param user_id: User ID.
        :param resource_type: Type of resource.
        :param permission_name: Required permission.
        :return: List of resource IDs.
        """
        accessible_ids = []

        # Personal resources: user is creator
        if resource_type == "project":
            personal_projects = (
                self.session.query(Project.id)
                .filter(
                    Project.user_id == user_id,
                    Project.organization_id.is_(None),
                )
                .all()
            )
            accessible_ids.extend([p[0] for p in personal_projects])

        # Org resources: check via RBAC
        # Get teams user belongs to
        team_ids = (
            self.session.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .all()
        )
        team_id_strs = [str(team_id[0]) for team_id in team_ids]

        # Get resource access entries for user/teams
        resource_access_entries = (
            self.session.query(ResourceAccess)
            .filter(
                ResourceAccess.resource_type == resource_type,
                or_(
                    and_(
                        ResourceAccess.grantee_type == "user",
                        ResourceAccess.grantee_id == user_id,
                    ),
                    and_(
                        ResourceAccess.grantee_type == "team",
                        ResourceAccess.grantee_id.in_(team_id_strs),
                    ),
                ),
            )
            .all()
        )

        # Filter by permission
        from orchestra.db.dao.role_dao import RoleDAO

        role_dao = RoleDAO(self.session)

        for entry in resource_access_entries:
            if role_dao.has_permission(entry.role_id, permission_name):
                accessible_ids.append(entry.resource_id)

        return list(set(accessible_ids))  # Remove duplicates
