"""Data Access Object for ResourceAccess model."""
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Project, ResourceAccess, TeamMember


class ResourceAccessDAO:
    """DAO for managing resource access (RBAC)."""

    # Module-level cache for permission checks (shared across instances)
    # Key: (user_id, resource_type, resource_id, permission_name)
    # Value: bool (has permission)
    _permission_cache = {}
    _cache_size_limit = 10000  # Prevent unbounded growth

    def __init__(self, session: Session):
        self.session = session

    @classmethod
    def clear_permission_cache(cls):
        """Clear the permission cache. Call this when roles/memberships change."""
        cls._permission_cache.clear()

    @classmethod
    def _get_cache_key(
        cls,
        user_id: str,
        resource_type: str,
        resource_id: int,
        permission_name: str,
    ) -> str:
        """Generate cache key for permission check."""
        return f"{user_id}:{resource_type}:{resource_id}:{permission_name}"

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

        Clears the permission cache since permissions have changed.

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
        # Clear cache since permissions changed
        self.clear_permission_cache()
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

        Clears the permission cache since permissions have changed.

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
        # Clear cache since permissions changed
        self.clear_permission_cache()

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

        Uses caching to improve performance. Cache is cleared when calling
        clear_permission_cache().

        :param user_id: User ID.
        :param resource_type: Type of resource ('project', 'interface', 'tab', 'tile').
        :param resource_id: Resource ID.
        :param permission_name: Permission name (e.g., 'project:read').
        :return: True if user has permission, False otherwise.
        """
        # Check cache first
        cache_key = self._get_cache_key(
            user_id,
            resource_type,
            resource_id,
            permission_name,
        )
        if cache_key in self._permission_cache:
            return self._permission_cache[cache_key]

        # Cache miss - compute permission
        # Step 1: Check if this is a personal resource
        if self._is_personal_resource(resource_type, resource_id):
            # Personal resource: creator has all permissions
            result = self._check_personal_ownership(resource_type, resource_id, user_id)
        else:
            # Step 2: Check org resource access via RBAC
            result = self._check_org_permission(
                user_id,
                resource_type,
                resource_id,
                permission_name,
            )

        # Store in cache (with size limit check)
        if len(self._permission_cache) >= self._cache_size_limit:
            # Simple eviction: clear entire cache when limit reached
            # Could be improved with LRU eviction, but this is simple and effective
            self._permission_cache.clear()

        self._permission_cache[cache_key] = result
        return result

    def check_user_has_permission_in_org(
        self,
        user_id: str,
        organization_id: int,
        permission_name: str,
    ) -> bool:
        """
        Check if user's role in an organization includes a specific permission.

        This is different from check_user_permission() which checks permissions
        on a specific resource. This checks what permissions the user WOULD have
        based on their organization membership role.

        Use case: When transferring a personal project to an org, check if the user
        has the necessary permissions in that org (via their membership role).

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :param permission_name: Permission name (e.g., 'project:write').
        :return: True if user's org role includes permission, False otherwise.
        """
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.models.orchestra_models import Permission, RolePermission

        org_member_dao = OrganizationMemberDAO(self.session)

        # Get user's membership
        membership = org_member_dao.get_member(user_id, organization_id)
        if not membership:
            return False

        # role_id should always be set explicitly (NOT NULL in database)
        if not membership.role_id:
            # This should never happen after migration
            raise ValueError(
                f"Organization member {user_id} in org {organization_id} has no role_id. "
                "This indicates a data integrity issue - all members must have explicit roles.",
            )
        role_id = membership.role_id

        # Check if role has the permission
        permission_exists = (
            self.session.query(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .filter(
                RolePermission.role_id == role_id,
                Permission.name == permission_name,
            )
            .first()
        )

        return permission_exists is not None

    def _is_personal_resource(self, resource_type: str, resource_id: int) -> bool:
        """
        Check if a resource is personal (not associated with an organization).

        Only project and org resources are supported.
        Projects can be personal (user_id set, organization_id NULL).
        Organizations are never personal.

        :param resource_type: Type of resource ("project" or "org").
        :param resource_id: Resource ID.
        :return: True if personal, False if organizational.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.organization_id is None
        elif resource_type == "org":
            # Organizations are never personal
            return False
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'org' are supported.",
            )

    def _check_personal_ownership(
        self,
        resource_type: str,
        resource_id: int,
        user_id: str,
    ) -> bool:
        """
        Check if user owns a personal resource.

        Only personal projects have ownership.
        Organizations cannot be personal.

        :param resource_type: Type of resource ("project" only for personal).
        :param resource_id: Resource ID.
        :param user_id: User ID.
        :return: True if user is owner, False otherwise.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.user_id == user_id
        elif resource_type == "org":
            # Organizations cannot be personal
            return False
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'org' are supported.",
            )

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
        0. Organization owner always has full permissions (checked first)
        1. Check if resource has ANY explicit grants
        2. If YES: Only check explicit grants for this user (no implicit fallback)
        3. If NO: Apply implicit organization membership access

        :param user_id: User ID.
        :param resource_type: Type of resource.
        :param resource_id: Resource ID.
        :param permission_name: Permission name.
        :return: True if user has permission, False otherwise.
        """
        from orchestra.db.dao.organization_dao import OrganizationDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.role_dao import RoleDAO

        role_dao = RoleDAO(self.session)

        # Step 0: Check if user is organization owner FIRST (before explicit grants)
        # Organization owners always have full permissions via their Owner role
        org_id = self._get_resource_organization_id(resource_type, resource_id)

        if org_id is not None:
            org_dao = OrganizationDAO(self.session)
            org = org_dao.get(org_id)

            if org and org.owner_id == user_id:
                # Organization owner has implicit full permissions via Owner role
                owner_role = role_dao.get_by_name("Owner", organization_id=None)
                if owner_role and role_dao.has_permission(
                    owner_role.id,
                    permission_name,
                ):
                    return True  # Owner always has access

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
            # (Org owner already checked above, so this only applies to non-owners)
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
        # (Org owner already checked above in Step 0)
        if org_id is None:
            return False

        # Check if user is organization member (use their assigned role)
        org_member_dao = OrganizationMemberDAO(self.session)
        member_obj = org_member_dao.get_member(user_id, org_id)

        if member_obj:
            # role_id should always be set explicitly (NOT NULL in database)
            if not member_obj.role_id:
                # This should never happen after migration
                raise ValueError(
                    f"Organization member {user_id} in org {org_id} has no role_id. "
                    "This indicates a data integrity issue - all members must have explicit roles.",
                )

            member_role_id = member_obj.role_id

            if role_dao.has_permission(member_role_id, permission_name):
                return True

        return False

    def _get_resource_organization_id(
        self,
        resource_type: str,
        resource_id: int,
    ) -> Optional[int]:
        """
        Get the organization ID for a resource.

        Projects have organization_id directly.
        For "org" type, the resource_id IS the organization_id.

        :param resource_type: Type of resource ("project" or "org").
        :param resource_id: Resource ID.
        :return: Organization ID or None if personal/not found.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project.organization_id if project else None
        elif resource_type == "org":
            # For organization resources, the resource_id IS the organization_id
            return resource_id
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'org' are supported.",
            )

    def filter_accessible_resources(
        self,
        user_id: str,
        resource_type: str,
        permission_name: str,
    ) -> List[int]:
        """
        Get IDs of all resources of a given type that user can access.

        Returns both personal and organizational resource IDs.
        Only "project" and "org" resource types are supported.

        :param user_id: User ID.
        :param resource_type: Type of resource ("project" or "org").
        :param permission_name: Required permission.
        :return: List of resource IDs.
        :raises ValueError: If resource_type is not supported.
        """
        # Validate resource type
        if resource_type not in ("project", "org"):
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'org' are supported.",
            )

        accessible_ids = []

        # Personal resources: only projects can be personal
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
