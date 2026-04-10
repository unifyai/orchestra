"""Shared fixtures and helpers for dashboard token tests."""

from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO


def make_project(dbsession: Session, user_id: str, name: str, organization_id=None):
    """Create a project via DAO and return the ORM object."""
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    project_dao.create(
        name=name,
        user_id=user_id,
        organization_id=organization_id,
    )
    dbsession.commit()
    rows = project_dao.filter(user_id=user_id, name=name)
    return rows[0][0]


def token_body(
    token: str,
    entity_type: str,
    context_name: str,
    project_name: str,
) -> dict:
    """Build a RegisterTokenRequest JSON body."""
    return {
        "token": token,
        "entity_type": entity_type,
        "context_name": context_name,
        "project_name": project_name,
    }
