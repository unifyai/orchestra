from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import ApiKey


class ApiKeyDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        key: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> None:

        if user_id is None and organization_id is None:
            raise ValueError("One of user_id or organization_id must be provided.")

        self.session.add(
            ApiKey(
                name=name,
                user_id=user_id,
                organization_id=organization_id,
                key=key,
            ),
        )

    def filter(
        self,
        id: Optional[id] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        key: Optional[str] = None,
    ) -> List[ApiKey]:
        query = select(ApiKey)
        if id:
            query = query.where(ApiKey.id == id)
        if user_id:
            query = query.where(ApiKey.user_id == user_id)
        if organization_id:
            query = query.where(ApiKey.organization_id == organization_id)
        if key:
            query = query.where(ApiKey.key == key)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        organization_id: Optional[id] = None,
    ) -> None:
        query = select(ApiKey)
        query = query.where(ApiKey.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if name:
                setattr(entry, "name", name)
            if user_id:
                setattr(entry, "user_id", user_id)
            if organization_id:
                setattr(entry, "organization_id", organization_id)

    def delete(self, id: int):
        try:
            api_key = self.session.query(ApiKey).filter_by(id=id).one()
            self.session.delete(api_key)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
