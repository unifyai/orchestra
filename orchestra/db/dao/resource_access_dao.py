"""Data Access Object for ResourceAccess model."""
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    Project,
    ResourceAccess,
    TeamMember,
)


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
        Grant or update access to a resource for a user or team.

        If the grantee already has access to this resource, their role is updated
        (upsert behavior). Only one role per grantee per resource is allowed.

        Clears the permission cache since permissions have changed.

        :param resource_type: Type of resource ('project', 'interface', etc.).
        :param resource_id: Resource ID.
        :param role_id: Role ID to grant.
        :param grantee_type: 'user' or 'team'.
        :param grantee_id: User ID or Team ID (as string).
        :return: The created or updated ResourceAccess object.
        """
        # Check for existing grant (any role) - upsert behavior
        existing = (
            self.session.query(ResourceAccess)
            .filter(
                ResourceAccess.resource_type == resource_type,
                ResourceAccess.resource_id == resource_id,
                ResourceAccess.grantee_type == grantee_type,
                ResourceAccess.grantee_id == grantee_id,
            )
            .first()
        )

        if existing:
            # Update existing grant's role
            existing.role_id = role_id
            self.session.flush()
            self.clear_permission_cache()
            return existing

        # Create new grant
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

    def get(self, access_id: int) -> Optional[ResourceAccess]:
        """
        Get a single resource access entry by ID.

        :param access_id: ResourceAccess ID.
        :return: ResourceAccess object or None.
        """
        return self.session.query(ResourceAccess).filter_by(id=access_id).first()

    def update_role(self, access_id: int, new_role_id: int) -> Optional[ResourceAccess]:
        """
        Update the role of an existing resource access grant.

        Clears the permission cache since permissions have changed.

        Note: Since only one role per grantee per resource is allowed (enforced by
        the unique constraint), there's no need to check for duplicates.

        :param access_id: ResourceAccess ID.
        :param new_role_id: New role ID to assign.
        :return: Updated ResourceAccess object or None if not found.
        """
        access = self.get(access_id)
        if not access:
            return None

        # Update role (no duplicate check needed - constraint enforces single role)
        access.role_id = new_role_id
        self.session.flush()

        # Clear cache since permissions changed
        self.clear_permission_cache()

        return access

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

        For personal resources: User is owner if resource.user_id == user_id.
        For org resources: Check ResourceAccess + team memberships.

        Uses caching to improve performance. Cache is cleared when calling
        clear_permission_cache().

        NOTE: For org-level operations (managing members, teams, invites),
        use check_org_member_permission() instead. This method is for
        resource-level access control (projects, assistants).

        :param user_id: User ID.
        :param resource_type: Type of resource ('project', 'assistant').
        :param resource_id: Resource ID (project.id or assistant.agent_id).
        :param permission_name: Permission name (e.g., 'project:read', 'assistant:write').
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

    def check_org_member_permission(
        self,
        user_id: str,
        organization_id: int,
        permission_name: str,
    ) -> bool:
        """
        Check if user's organization membership role grants a specific permission.

        This is the preferred method for org-level operations (managing members,
        teams, invites, org settings) as it directly uses the OrganizationMember.role_id.

        For resource-level access (e.g., project access), use check_user_permission()
        with resource_type="project" instead.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :param permission_name: Permission name (e.g., 'org:write', 'org:read').
        :return: True if user has permission, False otherwise.
        """
        from orchestra.db.dao.organization_dao import OrganizationDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.role_dao import RoleDAO

        org_dao = OrganizationDAO(self.session)
        org_member_dao = OrganizationMemberDAO(self.session)
        role_dao = RoleDAO(self.session)

        # Check if org exists
        org = org_dao.get(organization_id)
        if not org:
            return False

        # Org owner always has full permissions (check via Owner role)
        if org.owner_id == user_id:
            owner_role = role_dao.get_by_name("Owner", organization_id=None)
            if owner_role and role_dao.has_permission(owner_role.id, permission_name):
                return True

        # Check user's org membership role
        member = org_member_dao.get_member(user_id, organization_id)
        if not member:
            return False  # Not a member

        if not member.role_id:
            raise ValueError(
                f"Organization member {user_id} in org {organization_id} has no role_id. "
                "This indicates a data integrity issue - all members must have explicit roles.",
            )

        return role_dao.has_permission(member.role_id, permission_name)

    def _is_personal_resource(self, resource_type: str, resource_id: int) -> bool:
        """
        Check if a resource is personal (not associated with an organization).

        Supported resource types:
        - "project": personal if organization_id is NULL
        - "assistant": personal if organization_id is NULL

        :param resource_type: Type of resource ("project", "assistant").
        :param resource_id: Resource ID.
        :return: True if personal, False if organizational.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.organization_id is None
        elif resource_type == "assistant":
            assistant = (
                self.session.query(Assistant).filter_by(agent_id=resource_id).first()
            )
            return assistant is not None and assistant.organization_id is None
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'assistant' are supported. For org-level permissions, "
                "use check_org_member_permission() instead.",
            )

    def _check_personal_ownership(
        self,
        resource_type: str,
        resource_id: int,
        user_id: str,
    ) -> bool:
        """
        Check if user owns a personal resource.

        Supported resource types:
        - "project": ownership determined by project.user_id
        - "assistant": ownership determined by assistant.user_id

        :param resource_type: Type of resource ("project", "assistant").
        :param resource_id: Resource ID.
        :param user_id: User ID.
        :return: True if user is owner, False otherwise.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project is not None and project.user_id == user_id
        elif resource_type == "assistant":
            assistant = (
                self.session.query(Assistant).filter_by(agent_id=resource_id).first()
            )
            return assistant is not None and assistant.user_id == user_id
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'assistant' are supported. For org-level permissions, "
                "use check_org_member_permission() instead.",
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

        Supported resource types:
        - "project": has organization_id directly
        - "assistant": has organization_id directly

        :param resource_type: Type of resource ("project", "assistant").
        :param resource_id: Resource ID.
        :return: Organization ID or None if personal/not found.
        :raises ValueError: If resource_type is not supported.
        """
        if resource_type == "project":
            project = self.session.query(Project).filter_by(id=resource_id).first()
            return project.organization_id if project else None
        elif resource_type == "assistant":
            assistant = (
                self.session.query(Assistant).filter_by(agent_id=resource_id).first()
            )
            return assistant.organization_id if assistant else None
        else:
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'assistant' are supported. For org-level permissions, "
                "use check_org_member_permission() instead.",
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
        Supported resource types: "project", "assistant".

        :param user_id: User ID.
        :param resource_type: Type of resource ("project", "assistant").
        :param permission_name: Required permission.
        :return: List of resource IDs.
        :raises ValueError: If resource_type is not supported.
        """
        # Validate resource type
        if resource_type not in ("project", "assistant"):
            raise ValueError(
                f"Unsupported resource type: {resource_type}. "
                "Only 'project' and 'assistant' are supported. For org-level permissions, "
                "use check_org_member_permission() instead.",
            )

        accessible_ids = []

        # Personal resources
        if resource_type == "project":
            personal_resources = (
                self.session.query(Project.id)
                .filter(
                    Project.user_id == user_id,
                    Project.organization_id.is_(None),
                )
                .all()
            )
            accessible_ids.extend([r[0] for r in personal_resources])
        elif resource_type == "assistant":
            personal_resources = (
                self.session.query(Assistant.agent_id)
                .filter(
                    Assistant.user_id == user_id,
                    Assistant.organization_id.is_(None),
                )
                .all()
            )
            accessible_ids.extend([r[0] for r in personal_resources])

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

    def revoke_user_access_for_organization(
        self,
        user_id: str,
        organization_id: int,
    ) -> int:
        """
        Revoke all resource access for a user within an organization.
        Called when a member is removed from an organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :returns: Count of deleted entries.
        """
        # Get all project IDs in this org
        project_ids = [
            p.id
            for p in self.session.query(Project.id)
            .filter_by(organization_id=organization_id)
            .all()
        ]

        # Get all assistant IDs in this org
        assistant_ids = [
            a.agent_id
            for a in self.session.query(Assistant.agent_id)
            .filter_by(organization_id=organization_id)
            .all()
        ]

        # Delete user's access grants for these resources
        deleted = 0
        if project_ids:
            deleted += (
                self.session.query(ResourceAccess)
                .filter(
                    ResourceAccess.grantee_type == "user",
                    ResourceAccess.grantee_id == user_id,
                    ResourceAccess.resource_type == "project",
                    ResourceAccess.resource_id.in_(project_ids),
                )
                .delete(synchronize_session=False)
            )

        if assistant_ids:
            deleted += (
                self.session.query(ResourceAccess)
                .filter(
                    ResourceAccess.grantee_type == "user",
                    ResourceAccess.grantee_id == user_id,
                    ResourceAccess.resource_type == "assistant",
                    ResourceAccess.resource_id.in_(assistant_ids),
                )
                .delete(synchronize_session=False)
            )

        self.session.flush()
        self.clear_permission_cache()
        return deleted

    def delete_unshared_resources_by_creator(
        self,
        user_id: str,
        organization_id: int,
    ) -> dict:
        """
        Delete resources created by a user that were never shared with others.

        A resource is considered "unshared" if:
        - It has explicit ResourceAccess entries (not relying on implicit org access)
        - The ONLY grantee is the creator themselves

        Resources with no ResourceAccess entries use implicit org membership
        access and are considered shared with all org members.

        :param user_id: User ID of the creator being removed.
        :param organization_id: Organization ID.
        :returns: Dict with counts of deleted resources by type.
        """
        deleted = {"projects": 0, "assistants": 0}

        # Get resources created by this user in this org
        user_projects = (
            self.session.query(Project)
            .filter(
                Project.user_id == user_id,
                Project.organization_id == organization_id,
            )
            .all()
        )

        user_assistants = (
            self.session.query(Assistant)
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id == organization_id,
            )
            .all()
        )

        # Check each project
        for project in user_projects:
            if self._is_resource_unshared(
                resource_type="project",
                resource_id=project.id,
                creator_id=user_id,
            ):
                self.session.delete(project)
                deleted["projects"] += 1

        # Check each assistant
        for assistant in user_assistants:
            if self._is_resource_unshared(
                resource_type="assistant",
                resource_id=assistant.agent_id,
                creator_id=user_id,
            ):
                # Delete assistant logs before deleting assistant
                self._delete_assistant_logs(assistant, organization_id)
                self.session.delete(assistant)
                deleted["assistants"] += 1

        self.session.flush()
        return deleted

    def _delete_assistant_logs(
        self,
        assistant: Assistant,
        organization_id: int,
    ) -> None:
        """
        Delete all logs associated with an assistant being removed.

        Uses context_dao.delete() which handles:
        - Context deletion with cascade
        - 3-tier sibling cleanup (All/*, User/All/*)
        - GCS media cleanup
        - Orphaned log event cleanup
        """
        from orchestra.db.dao.context_dao import ContextDAO

        ASSISTANTS_PROJECT_NAME = "Assistants"

        org_project = (
            self.session.query(Project)
            .filter(
                Project.organization_id == organization_id,
                Project.name == ASSISTANTS_PROJECT_NAME,
            )
            .first()
        )

        if not org_project:
            return

        assistant_context_prefix = f"{assistant.first_name}{assistant.surname}"
        context_dao = ContextDAO(self.session)

        # Find assistant-specific contexts (both 2-tier and 3-tier patterns)
        from sqlalchemy import or_

        contexts_to_delete = (
            self.session.query(Context)
            .filter(
                Context.project_id == org_project.id,
                or_(
                    # Old 2-tier patterns
                    Context.name == assistant_context_prefix,
                    Context.name.like(f"{assistant_context_prefix}/%"),
                    # New 3-tier patterns: User/Assistant or User/Assistant/*
                    Context.name.like(f"%/{assistant_context_prefix}"),
                    Context.name.like(f"%/{assistant_context_prefix}/%"),
                ),
            )
            .all()
        )

        for ctx in contexts_to_delete:
            # context_dao.delete() handles:
            # - Sibling cleanup (All/*, User/All/*)
            # - GCS media cleanup
            # - Orphaned log event cleanup
            context_dao.delete(ctx.id)

    def _is_resource_unshared(
        self,
        resource_type: str,
        resource_id: int,
        creator_id: str,
    ) -> bool:
        """
        Check if a resource was never shared (only creator has explicit access).

        Returns True (unshared) if:
        - Resource has explicit ResourceAccess entries
        - ALL entries are for the creator only (no team grants, no other users)

        Returns False (shared) if:
        - Resource has NO explicit entries (uses implicit org access = shared with all)
        - Resource has entries for other users or teams
        """
        access_entries = self.get_resource_access(resource_type, resource_id)

        # No explicit grants = uses implicit org membership access = shared
        if not access_entries:
            return False

        # Check if all entries are for the creator only
        for entry in access_entries:
            # Team grant = shared
            if entry.grantee_type == "team":
                return False
            # Another user = shared
            if entry.grantee_type == "user" and entry.grantee_id != creator_id:
                return False

        # Only the creator has explicit access = unshared/private
        return True
