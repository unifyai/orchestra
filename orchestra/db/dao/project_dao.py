import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import (
    Context,
    ContextHistory,
    Project,
    ProjectVersion,
)


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
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self.session.add(
            Project(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
                is_versioned=is_versioned,
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
    ) -> None:
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

    def rename(self, user_id: str, name: str, new_name: str):
        try:
            project_id = self.filter(user_id=user_id, name=name)[0][0].id
        except:
            raise ValueError

        self.update(id=project_id, name=new_name)

    def delete(self, id: int):
        try:
            project = self.session.query(Project).filter_by(id=id).one()
            self.session.delete(project)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete project with id {id}", e)

    def filter_by_user_access(
        self,
        user_id: str,
        id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> List[Project]:
        """
        Filter projects that a user has access to, either as owner or through organization membership.

        Args:
            user_id: The ID of the user
            id: Optional project ID filter
            name: Optional project name filter

        Returns:
            List of projects the user has access to
        """
        # Get all organization IDs the user is a member of
        org_memberships = self.organization_member_dao.filter(user_id=user_id)
        org_ids = (
            [membership[0].organization_id for membership in org_memberships]
            if org_memberships
            else []
        )

        # Build query with access conditions
        query = select(Project).where(
            or_(
                Project.user_id == user_id,
                Project.organization_id.in_(org_ids) if org_ids else False,
            ),
        )

        # Apply additional filters if provided
        if id:
            query = query.where(Project.id == id)
        if name:
            query = query.where(Project.name == name)

        rows = self.session.execute(query)
        return rows.fetchall()

    def get_by_user_and_name(self, user_id: str, name: str) -> Optional[Project]:
        """
        Get a project by name that a user has access to.

        Args:
            user_id: The ID of the user
            name: The name of the project

        Returns:
            The project if found, None otherwise
        """
        projects = self.filter_by_user_access(user_id=user_id, name=name)
        return projects[0][0] if projects else None

    def commit(self, project_id: int, commit_message: Optional[str] = None) -> str:
        """
        Create a new version of a project.

        Args:
            project_id: The ID of the project to commit.
            commit_message: An optional message for the commit.

        Returns:
            The commit hash of the new version.
        """
        project = self.session.query(Project).filter_by(id=project_id).one_or_none()
        if not project or not project.is_versioned:
            raise ValueError("Project is not versioned.")

        # Generate commit hash
        commit_hash = hashlib.sha256(
            f"{project_id}{datetime.now(timezone.utc)}".encode(),
        ).hexdigest()

        # Create project version
        project_version = ProjectVersion(
            project_id=project_id,
            commit_hash=commit_hash,
            commit_message=commit_message,
        )
        self.session.add(project_version)

        # Increment project version
        project.version += 1
        project.updated_at = datetime.now(timezone.utc)

        # Commit all versioned contexts in the project
        contexts = (
            self.session.query(Context)
            .filter_by(project_id=project_id, is_versioned=True)
            .all()
        )
        for context in contexts:
            self.context_dao.commit(context.id, commit_hash, commit_message)

        self.session.commit()
        return commit_hash

    def rollback(self, project_id: int, commit_hash: str) -> None:
        """
        Rollback a project to a specific version.

        Args:
            project_id: The ID of the project to rollback.
            commit_hash: The commit hash to rollback to.
        """
        project = self.session.query(Project).filter_by(id=project_id).one_or_none()
        if not project or not project.is_versioned:
            raise ValueError("Project is not versioned.")

        project_version = (
            self.session.query(ProjectVersion)
            .filter_by(project_id=project_id, commit_hash=commit_hash)
            .one_or_none()
        )
        if not project_version:
            raise ValueError(
                f"Commit hash {commit_hash} not found for project {project_id}.",
            )

        # Find all context histories for this project and commit hash
        context_histories = (
            self.session.query(ContextHistory)
            .join(Context)
            .filter(
                Context.project_id == project_id,
                ContextHistory.commit_hash == commit_hash,
            )
            .all()
        )

        for ch in context_histories:
            self.context_dao.rollback(context_id=ch.context_id, version=ch.version)

        project.version = project_version.id  # Or some other way to track the version
        project.updated_at = datetime.now(timezone.utc)
        self.session.commit()
