import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import (
    Context,
    ContextVersion,
    Log,
    LogEvent,
    LogEventLog,
    OrganizationMember,
    Project,
    ProjectVersion,
    ResourceAccess,
    TeamMember,
)
from orchestra.db.utils import get_next_order_value


class ProjectDAO:
    def __init__(
        self,
        session: Session,
        organization_member_dao: OrganizationMemberDAO,
        context_dao: ContextDAO,
    ):
        self.session = session
        self.organization_member_dao = organization_member_dao
        self.context_dao = context_dao

    def _validate_description(self, description: Optional[str]) -> None:
        """Validate description length."""
        if description is not None and len(description) > 256:
            raise ValueError("Description cannot exceed 256 characters")

    def _get_project(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Project]:
        """Internal method to get project by ID or by user_id and name."""
        if id is not None:
            query = select(Project).where(Project.id == id)
            return self.session.execute(query).scalars().first()
        elif user_id is not None and name is not None:
            return self.get_by_user_and_name(user_id=user_id, name=name)
        return None

    def get(self, id: int) -> Optional[Project]:
        """Get project by ID."""
        return self._get_project(id=id)

    def create(  # noqa: WPS211
        self,
        name: str,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        is_versioned: bool = False,
        description: Optional[str] = None,
        icon: Optional[str] = "folder",
        order: Optional[int] = None,
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self._validate_description(description)

        # Determine order: append to end by default
        where_conditions = []
        if user_id is not None:
            where_conditions.append(Project.user_id == user_id)
        if organization_id is not None:
            where_conditions.append(Project.organization_id == organization_id)

        order_value = get_next_order_value(
            session=self.session,
            model_class=Project,
            order=order,
            where_conditions=where_conditions,
        )

        self.session.add(
            Project(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
                is_versioned=is_versioned,
                description=description,
                icon=icon,
                order=order_value,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> List[Project]:

        query = select(Project)
        if id:
            query = query.where(Project.id == id)
        if user_id:
            query = query.where(Project.user_id == user_id)
        if organization_id:
            query = query.where(Project.organization_id == organization_id)
        if name:
            query = query.where(Project.name == name)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        icon: Optional[str] = None,
        description: Optional[str] = None,
        order: Optional[int] = None,
    ) -> None:
        self._validate_description(description)

        query = select(Project)
        query = query.where(Project.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)
            if description is not None:
                setattr(entry, "description", description)
            if icon is not None:
                setattr(entry, "icon", icon)
            if order is not None:
                setattr(entry, "order", order)

    def rename(
        self,
        user_id: str,
        name: str,
        new_name: str,
        description: Optional[str] = None,
    ):
        try:
            project_id = self.filter(user_id=user_id, name=name)[0][0].id
        except:
            raise ValueError

        self.update(id=project_id, name=new_name, description=description)

    def delete(self, id: int):
        try:
            project = self.session.query(Project).filter_by(id=id).one()

            # Delete associated GCS media BEFORE deleting the project
            log_dao = LogDAO(self.session, self.context_dao)
            log_events_subquery = (
                select(LogEvent.id).where(LogEvent.project_id == id).subquery()
            )
            logs_to_delete_query = (
                self.session.query(Log)
                .join(
                    LogEventLog,
                    LogEventLog.log_id == Log.id,
                )
                .filter(
                    LogEventLog.log_event_id.in_(select(log_events_subquery.c.id)),
                )
            )
            log_dao._bulk_delete_gcs_media(logs_to_delete_query)

            # Proceed with deleting the project (DB cascades will handle the rest)
            self.session.delete(project)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete project with id {id}: {e}")

    def filter_by_user_access(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
        id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> List[Project]:
        """
        Filter projects that a user has access to based on API key context.

        When organization_id is None (personal API key):
            - Returns only personal projects (user_id set, organization_id NULL)
            - Plus projects with explicit ResourceAccess grants (personal projects only)

        When organization_id is set (org API key):
            - Returns only projects for that specific organization
            - Via org membership OR explicit ResourceAccess grants

        Args:
            user_id: The ID of the user
            organization_id: API key context (None = personal, int = org-specific)
            id: Optional project ID filter
            name: Optional project name filter

        Returns:
            List of projects the user has access to
        """
        # Get project IDs the user has explicit access to via ResourceAccess
        # (either direct user grants or via team membership)
        team_memberships = (
            self.session.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .all()
        )
        team_id_strs = [str(tm[0]) for tm in team_memberships]

        explicit_access_query = self.session.query(ResourceAccess.resource_id).filter(
            ResourceAccess.resource_type == "project",
            or_(
                and_(
                    ResourceAccess.grantee_type == "user",
                    ResourceAccess.grantee_id == user_id,
                ),
                and_(
                    ResourceAccess.grantee_type == "team",
                    ResourceAccess.grantee_id.in_(team_id_strs),
                )
                if team_id_strs
                else False,
            ),
        )
        explicit_project_ids = [row[0] for row in explicit_access_query.all()]

        if organization_id is None:
            # Personal API key: only personal projects + explicitly granted personal projects
            query = select(Project).where(
                and_(
                    Project.organization_id.is_(None),  # Personal projects only
                    or_(
                        Project.user_id == user_id,  # Owned by user
                        Project.id.in_(explicit_project_ids)
                        if explicit_project_ids
                        else False,  # Explicitly granted
                    ),
                ),
            )
        else:
            # Org API key: only projects for this specific organization
            # Check if user is member of this org
            is_member = (
                self.session.query(OrganizationMember)
                .filter(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.organization_id == organization_id,
                )
                .first()
                is not None
            )

            if is_member:
                # User is member - show all org projects
                query = select(Project).where(
                    Project.organization_id == organization_id,
                )
            else:
                # User is not member - only show explicitly granted projects in this org
                query = select(Project).where(
                    and_(
                        Project.organization_id == organization_id,
                        Project.id.in_(explicit_project_ids)
                        if explicit_project_ids
                        else False,
                    ),
                )

        # Apply additional filters if provided
        if id:
            query = query.where(Project.id == id)
        if name:
            query = query.where(Project.name == name)

        rows = self.session.execute(query)
        return rows.fetchall()

    def get_by_user_and_name(
        self,
        user_id: str,
        name: str,
        organization_id: Optional[int] = None,
    ) -> Optional[Project]:
        """
        Get a project by name that a user has access to.

        Args:
            user_id: The ID of the user
            name: The name of the project
            organization_id: API key context (None = personal, int = org-specific)

        Returns:
            The project if found, None otherwise
        """
        projects = self.filter_by_user_access(
            user_id=user_id,
            organization_id=organization_id,
            name=name,
        )
        return projects[0][0] if projects else None

    def get_by_user_and_name_any_context(
        self,
        user_id: str,
        name: str,
    ) -> Optional[Project]:
        """
        Get a project by name that a user has access to (ignoring API key context).

        This is used for internal operations that need to find any accessible project
        regardless of the current API key context (e.g., project transfer operations).

        Args:
            user_id: The ID of the user
            name: The name of the project

        Returns:
            The project if found, None otherwise
        """
        # Get all organization IDs the user is a member of
        org_memberships = self.organization_member_dao.filter(user_id=user_id)
        org_ids = (
            [membership[0].organization_id for membership in org_memberships]
            if org_memberships
            else []
        )

        # Get explicit access grants
        team_memberships = (
            self.session.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .all()
        )
        team_id_strs = [str(tm[0]) for tm in team_memberships]

        explicit_access_query = self.session.query(ResourceAccess.resource_id).filter(
            ResourceAccess.resource_type == "project",
            or_(
                and_(
                    ResourceAccess.grantee_type == "user",
                    ResourceAccess.grantee_id == user_id,
                ),
                and_(
                    ResourceAccess.grantee_type == "team",
                    ResourceAccess.grantee_id.in_(team_id_strs),
                )
                if team_id_strs
                else False,
            ),
        )
        explicit_project_ids = [row[0] for row in explicit_access_query.all()]

        # Build query with all access conditions
        query = select(Project).where(
            and_(
                Project.name == name,
                or_(
                    Project.user_id == user_id,  # Personal ownership
                    Project.organization_id.in_(org_ids)
                    if org_ids
                    else False,  # Org membership
                    Project.id.in_(explicit_project_ids)
                    if explicit_project_ids
                    else False,  # Explicit grant
                ),
            ),
        )

        result = self.session.execute(query).fetchall()
        return result[0][0] if result else None

    def commit(self, project_id: int, commit_message: Optional[str] = None) -> str:
        """
        Create a new version of a project by taking a snapshot of all its
        versioned contexts.
        """
        project = self.session.query(Project).filter_by(id=project_id).one_or_none()
        if not project or not project.is_versioned:
            raise ValueError("Project is not versioned.")

        # Get the current HEAD commit
        current_head = project.current_commit_hash

        # 1. Generate a commit hash and create the ProjectVersion
        commit_hash = hashlib.sha256(
            f"{project_id}{datetime.now(timezone.utc)}".encode(),
        ).hexdigest()

        project_version = ProjectVersion(
            project_id=project_id,
            commit_hash=commit_hash,
            commit_message=commit_message,
            prev_commit_hash=current_head,
        )
        self.session.add(project_version)
        self.session.flush()  # Flush to get the project_version.id

        # Update the previous version's next_commit_hash array if it exists
        if current_head:
            prev_version = (
                self.session.query(ProjectVersion)
                .filter_by(
                    project_id=project_id,
                    commit_hash=current_head,
                )
                .with_for_update()
                .one()
            )
            if commit_hash not in prev_version.next_commit_hash:
                prev_version.next_commit_hash = prev_version.next_commit_hash + [
                    commit_hash,
                ]

        # 2. Find all versioned contexts and create a snapshot for each
        contexts = (
            self.session.query(Context)
            .filter_by(project_id=project_id, is_versioned=True)
            .all()
        )
        for context in contexts:
            self.context_dao.create_version_snapshot(
                context=context,
                commit_hash=commit_hash,
                commit_message=commit_message,
                project_version=project_version,
                prev_commit_hash=context.current_commit_hash,
            )
        project.updated_at = datetime.now(timezone.utc)

        # Update the project's HEAD pointer
        project.current_commit_hash = commit_hash

        self.session.commit()
        return commit_hash

    def rollback(self, project_id: int, commit_hash: str) -> None:
        """
        Rollback a project and all its versioned contexts to a specific commit.
        """
        project = self.session.query(Project).filter_by(id=project_id).one_or_none()
        if not project or not project.is_versioned:
            raise ValueError("Project is not versioned.")

        # 1. Find the target project version by its commit hash
        project_version = (
            self.session.query(ProjectVersion)
            .filter_by(project_id=project_id, commit_hash=commit_hash)
            .one_or_none()
        )
        if not project_version:
            raise ValueError(
                f"Commit hash {commit_hash} not found for project {project_id}.",
            )

        # 2. Find all context versions associated with this project version
        context_versions = (
            self.session.query(ContextVersion)
            .filter_by(project_version_id=project_version.id)
            .all()
        )

        # 3. Rollback each context to its respective versioned state
        for cv in context_versions:
            self.context_dao.rollback(cv.context_id, cv.commit_hash)

        project.updated_at = datetime.now(timezone.utc)

        # Move the HEAD pointer to the target commit
        project.current_commit_hash = commit_hash

        self.session.commit()

    def get_commit_history(self, project_id: int) -> List[dict]:
        """
        Retrieves the commit history for a versioned project.
        """
        project = self.session.query(Project).filter_by(id=project_id).one_or_none()
        if not project or not project.is_versioned:
            raise ValueError("Project is not versioned.")

        versions = (
            self.session.query(ProjectVersion)
            .filter_by(project_id=project_id)
            .order_by(ProjectVersion.created_at.desc())
            .all()
        )
        return [
            {
                "commit_hash": v.commit_hash,
                "commit_message": v.commit_message,
                "created_at": v.created_at.isoformat(),
                "prev_commit_hash": v.prev_commit_hash,
                "next_commit_hash": v.next_commit_hash,
            }
            for v in versions
        ]
