import hashlib
import json
from typing import Any, List, Optional

import shortuuid
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DatasetEntry


class DatasetEntryDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        dataset_id: int,
        entry: str,
    ) -> str:
        _id = shortuuid.ShortUUID().random(length=10)
        entry = json.dumps(entry)
        entry_hash = hashlib.sha256(entry.encode("utf-8")).hexdigest()

        new_dataset_entry = DatasetEntry(
            id=_id,
            dataset_id=dataset_id,
            entry=entry,
            entry_hash=entry_hash,
        )

        self.session.add(new_dataset_entry)
        self.session.commit()
        return new_dataset_entry.id

    def filter(
        self,
        id: Optional[int] = None,
        dataset_id: Optional[int] = None,
        entry: Optional[Any] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> List[DatasetEntry]:
        query = select(DatasetEntry.id, DatasetEntry.entry, DatasetEntry.created_at)
        if id:
            query = query.where(DatasetEntry.id == id)
        if entry:
            query = query.where(DatasetEntry.entry == json.dumps(entry))
        if dataset_id:
            query = query.where(DatasetEntry.dataset_id == dataset_id)

        query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        query = query.order_by(DatasetEntry.created_at)

        rows = self.session.execute(query)
        return rows.fetchall()

    def delete(self, id: str) -> None:
        try:
            dataset_entry = self.session.query(DatasetEntry).filter_by(id=id).one()
            self.session.delete(dataset_entry)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
