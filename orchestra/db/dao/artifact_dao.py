from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Artifact


class ArtifactDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        project_id: int,
        key: str,
        value: Optional[str] = None,
    ) -> None:

        self.session.add(
            Artifact(
                project_id=project_id,
                key=key,
                value=value,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        project_id: Optional[int] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> List[Artifact]:
        query = select(Artifact)
        if id:
            query = query.where(Artifact.id == id)
        if project_id:
            query = query.where(Artifact.project_id == project_id)
        if key:
            query = query.where(Artifact.key == key)
        if value:
            query = query.where(Artifact.value == value)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        key: Optional[str] = None,
        value: Optional[str] = None,
        project_id: Optional[int] = None,
    ) -> None:
        query = select(Artifact)
        query = query.where(Artifact.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if key:
                setattr(entry, "key", key)
            if value:
                setattr(entry, "value", value)
            if project_id:
                setattr(entry, "project_id", project_id)

    def delete(self, id: int):
        try:
            artifact = self.session.query(Artifact).filter_by(id=id).one()
            self.session.delete(artifact)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
