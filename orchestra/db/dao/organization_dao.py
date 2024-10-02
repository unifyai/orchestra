from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Organization


class OrganizationDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        name: str,
        owner_id: str,
    ) -> None:

        self.session.add(
            Organization(
                name=name,
                owner_id=owner_id,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        owner_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Organization]:
        query = select(Organization)
        if id:
            query = query.where(Organization.id == id)
        if owner_id:
            query = query.where(Organization.owner_id == owner_id)
        if name:
            query = query.where(Organization.name == name)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        owner_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        query = select()
        query = query.where(Organization.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if owner_id:
                setattr(entry, "owner_id", owner_id)

    def delete(self, id: int):
        try:
            org = self.session.query(Organization).filter_by(id=id).one()
            self.session.delete(org)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
