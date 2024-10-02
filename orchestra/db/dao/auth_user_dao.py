from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import AuthUser


class AuthUserDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        email: str,
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        image: Optional[str] = None,
    ) -> None:
        self.session.add(
            AuthUser(
                email=email,
                name=name,
                last_name=last_name,
                job_title=job_title,
                image=image,
            ),
        )

    def filter(
        self,
        id: Optional[str] = None,
        email: Optional[str] = None,
    ) -> List[AuthUser]:
        query = select(AuthUser)
        if id:
            query = query.where(AuthUser.id == id)
        if email:
            query = query.where(AuthUser.email == email)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(  # noqa: WPS211, WPS213, WPS231, C901
        self,
        id: int,  # noqa: WPS125
        name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        image: Optional[str] = None,
        tier: Optional[str] = None,
        queries_enabled: Optional[bool] = None,
        evaluations_enabled: Optional[bool] = None,
    ) -> None:
        query = select(AuthUser)
        query = query.where(AuthUser.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if last_name:
                setattr(entry, "last_name", last_name)
            if job_title:
                setattr(entry, "job_title", job_title)
            if image:
                setattr(entry, "image", image)
            if tier:
                setattr(entry, "tier", tier)
            if queries_enabled:
                setattr(entry, "queries_enabled", queries_enabled)
            if evaluations_enabled:
                setattr(entry, "evaluations_enabled", evaluations_enabled)

    def delete(self, id: str):
        try:
            auth_user = self.session.query(AuthUser).filter_by(id=id).one()
            self.session.delete(auth_user)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
