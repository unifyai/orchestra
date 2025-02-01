import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Log, LogEvent


class OverwriteError(Exception):
    pass


# noinspection PyBroadException
class LogDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        project_id: int,
        log_event_id: int,
        key: str,
        value: Optional[str] = None,  # JSON serialised
        version: Optional[int] = None,
        inferred_type: Optional[str] = None,
    ) -> int:

        if version:
            # Lock the rows for version check
            query = (
                select(Log)
                .join(LogEvent, Log.log_event_id == LogEvent.id)
                .where(
                    LogEvent.project_id == project_id,
                    Log.key == key,
                    Log.version == version,
                )
            )
            existing = self.session.execute(query).first()
            if existing and existing[0].value != value:
                raise ValueError("Version mismatch")

        ts = datetime.now(timezone.utc)

        new_log = Log(
            log_event_id=log_event_id,
            key=key,
            value=value,
            version=version,
            inferred_type=inferred_type,
            created_at=ts,
            updated_at=ts,
        )

        self.session.add(new_log)
        self.session.commit()
        return new_log.id

    @staticmethod
    def possible_img(raw_k):
        lower = raw_k.lower()
        return (
            "img" in lower
            or "image" in lower
            or "photo" in lower
            or "diagram" in lower
            or "pic" in lower
        )

    @staticmethod
    def infer_type(raw_k, raw_v):
        maybe_img = LogDAO.possible_img(raw_k)
        if isinstance(raw_v, str):
            try:
                datetime.fromisoformat(raw_v)
                return "timestamp"
            except:
                if not maybe_img:
                    return "str"
                binary = raw_v.encode("utf-8")
                try:
                    assert base64.b64encode(base64.b64decode(binary)) == binary
                    return "image"
                except:
                    lower = raw_v.lower()
                    if lower.startswith("http") and (
                        lower.endswith(".png")
                        or lower.endswith(".jpg")
                        or lower.endswith(".jpeg")
                    ):
                        return "image"
                    return "str"
        return type(raw_v).__name__

    def create_from_raw_k_v(
        self,
        project_id: int,
        log_event_id: int,
        raw_k: str,
        version: Optional[int] = None,
        raw_v: Optional[Any] = None,
        explicit_types: Optional[Dict] = None,
    ) -> Optional[str]:

        explicit_types = explicit_types if isinstance(explicit_types, dict) else {}

        return self.create(
            project_id=project_id,
            log_event_id=log_event_id,
            key=raw_k,
            value=raw_v,
            version=version,
            inferred_type=explicit_types.get(
                raw_k,
                self.infer_type(raw_k, raw_v),
            ),
        )

    def filter(
        self,
        id: Optional[Union[int, List[int]]] = None,
        log_event_id: Optional[Union[int, List[int]]] = None,
        key: Optional[Union[str, List[str]]] = None,
        value: Optional[Union[str, List[str]]] = None,
        version: Optional[Union[int, List[int]]] = None,
        inferred_type: Optional[Union[str, List[str]]] = None,
        project_id: Optional[int] = None,
        defer: bool = False,
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
        if project_id:
            query = query.where(LogEvent.project_id == project_id)

        query = query.order_by(Log.created_at)
        rows = self.session.execute(query)
        if defer:
            return rows
        return rows.fetchall()


    def update_value(
        self,
        log_event_id: int,
        raw_k: str,
        raw_v: Optional[Any] = None,
        version: Optional[int] = None,
        explicit_types: Optional[Dict] = None,
        overwrite: bool = False,
    ):

        inferred_type = type(raw_v).__name__
        json_v = raw_v

        if explicit_types and isinstance(explicit_types, Dict):
            if raw_k in explicit_types:
                inferred_type = explicit_types[raw_k]

        query = (
            select(Log).where(Log.log_event_id == log_event_id).where(Log.key == raw_k)
        )
        raw = self.session.execute(query)
        entry = raw.scalars().first()
        if entry is not None:
            if not overwrite and hasattr(entry, "value"):
                raise OverwriteError
            setattr(entry, "value", json_v)
            setattr(entry, "version", version)
            setattr(entry, "inferred_type", inferred_type)

            # Update the LogEvent's updated_at timestamp
            log_event_query = (
                select(LogEvent).where(LogEvent.id == log_event_id).with_for_update()
            )
            log_event = self.session.execute(log_event_query).scalars().first()
            if log_event:
                log_event.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        else:
            raise IndexError

    def delete(self, id: int):
        try:
            log = self.session.query(Log).filter_by(id=id).one()
            self.session.delete(log)
            self.session.commit()
        except:
            self.session.rollback()
            raise ValueError

