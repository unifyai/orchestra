from typing import List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.models.orchestra_models import Project, ResourceAccess, TeamMember
from orchestra_core.db.dao.context_dao import ContextDAO
from orchestra_core.db.dao.project_dao import ProjectDAO as CoreProjectDAO


class ProjectDAO(CoreProjectDAO):
    def __init__(
        self,
        session: Session,
        organization_member_dao: OrganizationMemberDAO,
        context_dao: ContextDAO,
    ):
        super().__init__(session, context_dao)
        self.organization_member_dao = organization_member_dao

    def filter_by_user_access(
        self,
        user_id: str,
        organization_id: Optional[int] = None,
        id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> List[Project]:
        """Filter projects visible under the platform API-key context."""
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
                (
                    and_(
                        ResourceAccess.grantee_type == "team",
                        ResourceAccess.grantee_id.in_(team_id_strs),
                    )
                    if team_id_strs
                    else False
                ),
            ),
        )
        explicit_project_ids = [row[0] for row in explicit_access_query.all()]

        if organization_id is None:
            query = select(Project).where(
                and_(
                    Project.organization_id.is_(None),
                    or_(
                        Project.user_id == user_id,
                        (
                            Project.id.in_(explicit_project_ids)
                            if explicit_project_ids
                            else False
                        ),
                    ),
                ),
            )
        else:
            query = select(Project).where(
                and_(
                    Project.organization_id == organization_id,
                    (
                        Project.id.in_(explicit_project_ids)
                        if explicit_project_ids
                        else False
                    ),
                ),
            )

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
        org_memberships = self.organization_member_dao.filter(user_id=user_id)
        org_ids = (
            [membership[0].organization_id for membership in org_memberships]
            if org_memberships
            else []
        )

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
                (
                    and_(
                        ResourceAccess.grantee_type == "team",
                        ResourceAccess.grantee_id.in_(team_id_strs),
                    )
                    if team_id_strs
                    else False
                ),
            ),
        )
        explicit_project_ids = [row[0] for row in explicit_access_query.all()]

        query = select(Project).where(
            and_(
                Project.name == name,
                or_(
                    Project.user_id == user_id,
                    Project.organization_id.in_(org_ids) if org_ids else False,
                    (
                        Project.id.in_(explicit_project_ids)
                        if explicit_project_ids
                        else False
                    ),
                ),
            ),
        )

        result = self.session.execute(query).fetchall()
        return result[0][0] if result else None
