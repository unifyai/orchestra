from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import OrganizationMember


class OrganizationMemberDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        organization_id: int,
        user_id: str,
        level: str,
    ) -> None:

        if level not in ["owner", "admin", "user"]:
            raise ValueError("User level must be one of [owner, admin, user].")

        self.session.add(
            OrganizationMember(
                user_id=user_id,
                organization_id=organization_id,
                level=level,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        level: Optional[str] = None,
    ) -> List[OrganizationMember]:
        query = select(OrganizationMember)
        if id:
            query = query.where(OrganizationMember.id == id)
        if user_id:
            query = query.where(OrganizationMember.user_id == user_id)
        if organization_id:
            query = query.where(OrganizationMember.organization_id == organization_id)
        if level:
            query = query.where(OrganizationMember.level == level)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        level: Optional[str] = None,
    ) -> None:
        query = select()
        query = query.where(OrganizationMember.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if level:
                setattr(entry, "level", level)
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)

    def delete(self, id: int):
        try:
            org_member = self.session.query(OrganizationMember).filter_by(id=id).one()
            self.session.delete(org_member)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
