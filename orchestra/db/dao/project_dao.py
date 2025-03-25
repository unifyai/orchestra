from typing import List, Optional

from fastapi import Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Project


class ProjectDAO:
    def __init__(
        self,
        session: Session = Depends(get_db_session),
        organization_member_dao: OrganizationMemberDAO = Depends(),
    ):
        self.session = session
        self.organization_member_dao = organization_member_dao

    def create(  # noqa: WPS211
        self,
        name: str,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self.session.add(
            Project(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
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
