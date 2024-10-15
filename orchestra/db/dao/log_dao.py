import json
from typing import Any, List, Optional, Union

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Log, LogEvent


class LogDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    # TODO: Add suffix and ensure that keys with the same suffix have the same value
    def create(
        self,
        log_event_id: int,
        key: str,
        value: Optional[str] = None,  # JSON serialised
        version: Optional[str] = None,
        inferred_type: Optional[str] = None,
    ) -> Optional[str]:

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

    def create_from_raw_k_v(
        self,
        log_event_id: int,
        raw_k: str,
        raw_v: Optional[Any] = None,
    ) -> Optional[str]:

        inferred_type = type(raw_v).__name__
        clean_key = raw_k.split("/", 1)
        json_v = json.dumps(raw_v)
        return self.create(
            log_event_id=log_event_id,
            key=clean_key[0],
            value=json_v,
            version=clean_key[1] if len(clean_key) > 1 else None,
            inferred_type=inferred_type,
        )

    def filter(
        self,
        id: Optional[Union[int, List[int]]] = None,
        log_event_id: Optional[Union[int, List[int]]] = None,
        key: Optional[Union[str, List[str]]] = None,
        value: Optional[Union[str, List[str]]] = None,
        version: Optional[Union[str, List[str]]] = None,
        inferred_type: Optional[Union[str, List[str]]] = None,
    ) -> List[Log]:
        def normalize_input(value):
            if value is None or isinstance(value, list):
                return value
            return [value]

        id = normalize_input(id)
        log_event_id = normalize_input(log_event_id)
        key = normalize_input(key)
        value = normalize_input(value)
        version = normalize_input(version)
        inferred_type = normalize_input(inferred_type)

        if (
            id == []
            or log_event_id == []
            or key == []
            or value == []
            or version == []
            or inferred_type == []
        ):
            return []

        query = select(Log, LogEvent.created_at.label("log_event_ts")).join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        if id:
            query = query.where(Log.id.in_(id))
        if log_event_id:
            query = query.where(Log.log_event_id.in_(log_event_id))
        if key:
            query = query.where(Log.key.in_(key))
        if value:
            query = query.where(Log.value.in_(value))
        if version:
            query = query.where(Log.version.in_(version))
        if inferred_type:
            query = query.where(Log.inferred_type.in_(inferred_type))
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
