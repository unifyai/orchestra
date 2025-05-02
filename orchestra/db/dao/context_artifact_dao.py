from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import ContextArtifact


class ContextArtifactDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        context_id: int,
        key: str,
        value: Optional[str] = None,
    ) -> None:
        self.session.add(
            ContextArtifact(
                context_id=context_id,
                key=key,
                value=value,
            ),
        )
        self.session.commit()

    def filter(
        self,
        id: Optional[int] = None,
        context_id: Optional[int] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> List[ContextArtifact]:
        query = select(ContextArtifact)
        if id:
            query = query.where(ContextArtifact.id == id)
        if context_id:
            query = query.where(ContextArtifact.context_id == context_id)
        if key:
            query = query.where(ContextArtifact.key == key)
        if value:
            query = query.where(ContextArtifact.value == value)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        key: Optional[str] = None,
        value: Optional[str] = None,
        context_id: Optional[int] = None,
    ) -> None:
        query = select(ContextArtifact)
        query = query.where(ContextArtifact.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if key:
                setattr(entry, "key", key)
            if value:
                setattr(entry, "value", value)
            if context_id:
                setattr(entry, "context_id", context_id)

    def delete(self, id: int):
        try:
            artifact = self.session.query(ContextArtifact).filter_by(id=id).one()
            self.session.delete(artifact)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
