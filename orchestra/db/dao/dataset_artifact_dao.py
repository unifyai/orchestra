from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DatasetArtifact


class DatasetArtifactDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(  # noqa: WPS211
        self,
        dataset_id: int,
        key: str,
        value: Optional[str] = None,
    ) -> None:

        self.session.add(
            DatasetArtifact(
                dataset_id=dataset_id,
                key=key,
                value=value,
            ),
        )

    def filter(
        self,
        id: Optional[int] = None,
        dataset_id: Optional[int] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> List[DatasetArtifact]:
        query = select(DatasetArtifact)
        if id:
            query = query.where(DatasetArtifact.id == id)
        if dataset_id:
            query = query.where(DatasetArtifact.dataset_id == dataset_id)
        if key:
            query = query.where(DatasetArtifact.key == key)
        if value:
            query = query.where(DatasetArtifact.value == value)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        key: Optional[str] = None,
        value: Optional[str] = None,
        project_id: Optional[int] = None,
    ) -> None:
        query = select(DatasetArtifact)
        query = query.where(DatasetArtifact.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if key:
                setattr(entry, "key", key)
            if value:
                setattr(entry, "value", value)
            if project_id:
                setattr(entry, "dataset_id", project_id)

    def delete(self, id: int):
        try:
            dataset_artifact = (
                self.session.query(DatasetArtifact).filter_by(id=id).one()
            )
            self.session.delete(dataset_artifact)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
