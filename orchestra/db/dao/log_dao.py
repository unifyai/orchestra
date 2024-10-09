from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Log


class LogDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    # TODO: Add suffix and ensure that keys with the same suffix have the same value
    def create(  # noqa: WPS211
        self,
        log_event_id: int,
        key: str,
        value: Optional[str] = None,  # JSON serialised
        version: Optional[str] = None,
        inferred_type: Optional[str] = None,
    ) -> None:

        new_log = Log(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
            inferred_type=inferred_type,
        )

        self.session.add(new_log)
        self.session.commit()
        return new_log.id

    def filter(
        self,
        id: Optional[int] = None,
        log_event_id: Optional[int] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
        version: Optional[str] = None,
        inferred_type: Optional[str] = None,
    ) -> List[Log]:
        query = select(Log)
        if id:
            query = query.where(Log.id == id)
        if log_event_id:
            query = query.where(Log.log_event_id == log_event_id)
        if key:
            query = query.where(Log.key == key)
        if value:
            query = query.where(Log.value == value)
        if version:
            query = query.where(Log.version == version)
        if inferred_type:
            query = query.where(Log.inferred_type == inferred_type)
        rows = self.session.execute(query)
        return rows.fetchall()

    def update(
        self,
        id: int,
        key: Optional[str] = None,
        value: Optional[str] = None,
        version: Optional[str] = None,
        inferred_type: Optional[str] = None,
        log_event_id: Optional[int] = None,
    ) -> None:
        query = select(Log)
        query = query.where(Log.id == id)
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if key:
                setattr(entry, "key", key)
            if value:
                setattr(entry, "value", value)
            if version:
                setattr(entry, "version", version)
            if inferred_type:
                setattr(entry, "inferred_type", inferred_type)
            if log_event_id:
                setattr(entry, "log_event_id", log_event_id)

    def delete(self, id: int):
        try:
            log = self.session.query(Log).filter_by(id=id).one()
            self.session.delete(log)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError
