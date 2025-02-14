from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DerivedLog, LogEvent
from orchestra.web.api.log.helpers import (
    _compute_expression,
    _extract_placeholders,
    _substitute_placeholders,
    str_filter_exp_to_dict,
)


class OverwriteError(Exception):
    pass


def _transform_referenced_logs(equation: str, referenced_logs: Dict) -> List[Dict]:
    """
    Transform referenced_logs to use log placeholders (log0, log1, etc.) as keys.

    Args:
        equation: String containing placeholders like '{log0:a}+1 + {log1:b}'
        referenced_logs: Dict with original keys, e.g. {'a': 1, 'b': 2}

    Returns:
        Dict with transformed keys, e.g. {'log0': 1, 'log1': 2}
    """
    # Extract placeholders and their original keys
    placeholders = _extract_placeholders(equation)  # ['log0:a', 'log1:a']

    # Create transformed dictionary
    transformed = {}
    for p in placeholders:
        log_key, original_key = p.split(":")  # 'log0:a' -> ('log0', 'a')
        transformed[log_key] = referenced_logs[original_key]

    return transformed


# noinspection PyBroadException
class DerivedLogDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        log_event_id: int,
        key: str,
        equation: str,
        referenced_logs: Dict[str, int],
        value: int,
        inferred_type: str,
    ) -> int:

        ts = datetime.now(timezone.utc)

        new_derived_log = DerivedLog(
            log_event_id=log_event_id,
            key=key,
            equation=equation,
            referenced_logs=referenced_logs,
            value=value,
            inferred_type=inferred_type,
            created_at=ts,
            updated_at=ts,
        )

        self.session.add(new_derived_log)
        self.session.commit()
        return new_derived_log.id

    def filter(
        self,
        id: Optional[Union[int, List[int]]] = None,
        log_event_id: Optional[Union[int, List[int]]] = None,
        key: Optional[Union[str, List[str]]] = None,
        value: Optional[Union[str, List[str]]] = None,
        equation: Optional[Union[str, List[str]]] = None,
        project_id: Optional[int] = None,
        defer: bool = False,
    ) -> List[DerivedLog]:
        def normalize_input(value):
            if value is None or isinstance(value, list):
                return value
            return [value]

        id = normalize_input(id)
        log_event_id = normalize_input(log_event_id)
        key = normalize_input(key)
        value = normalize_input(value)
        equation = normalize_input(equation)

        if id == [] or log_event_id == [] or key == [] or value == [] or equation == []:
            return []

        query = select(DerivedLog).join(
            LogEvent,
            LogEvent.id == DerivedLog.log_event_id,
        )
        if id:
            query = query.where(DerivedLog.id.in_(id))
        if log_event_id:
            query = query.where(DerivedLog.log_event_id.in_(log_event_id))
        if key:
            query = query.where(DerivedLog.key.in_(key))
        if value:
            query = query.where(DerivedLog.value.in_(value))
        if equation:
            query = query.where(DerivedLog.equation.in_(equation))
        if project_id:
            query = query.where(LogEvent.project_id == project_id)

        query = query.order_by(DerivedLog.created_at)
        rows = self.session.execute(query)
        if defer:
            return rows
        return rows.fetchall()

    def recompute_derived_logs(
        self,
        logs_to_recompute: List[DerivedLog],
        session: Session,
    ) -> None:
        """
        Recompute the 'value' (and optionally type) for each derived log in logs_to_recompute,
        based on the log's 'equation' and 'referenced_logs'. Then commit the changes.
        """
        try:
            for dlog in logs_to_recompute:
                transformed_logs = _transform_referenced_logs(
                    dlog.equation,
                    dlog.referenced_logs,
                )
                filter_expr, alias_to_key_map = _substitute_placeholders(
                    dlog.equation,
                    transformed_logs,
                )
                log_event_ids = {
                    alias_to_key_map[k]: [v] for k, v in transformed_logs.items()
                }
                filter_dict = str_filter_exp_to_dict(filter_expr)
                new_val = _compute_expression(
                    filter_dict,
                    LogEvent,
                    session,
                    log_event_ids,
                )[0][1]
                dlog.value = new_val
                dlog.updated_at = datetime.now(timezone.utc)

            session.commit()
        except Exception as e:
            session.rollback()
            raise e

    def update(
        self,
        id: int,
        key: str = None,
        equation: str = None,
    ) -> DerivedLog:
        """Update a derived log entry by ID"""
        try:
            derived_log = self.session.query(DerivedLog).get(id)
            if not derived_log:
                raise ValueError(f"No derived log found with id {id}")

            # Check for key conflicts
            if key and key != derived_log.key:
                exists = (
                    self.session.query(DerivedLog)
                    .filter(
                        DerivedLog.log_event_id == derived_log.log_event_id,
                        DerivedLog.key == key,
                    )
                    .first()
                )
                if exists:
                    raise ValueError(f"Key '{key}' already exists for this log event")

            # Apply updates
            if key:
                derived_log.key = key
            if equation:
                derived_log.equation = equation
            derived_log.updated_at = datetime.now(timezone.utc)

            self.session.commit()
            return derived_log
        except Exception as e:
            self.session.rollback()
            raise e

    def delete(self, id: int) -> None:
        """Delete a derived log entry by ID"""
        try:
            derived_log = self.session.query(DerivedLog).get(id)
            if not derived_log:
                raise ValueError(f"No derived log found with id {id}")

            self.session.delete(derived_log)
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            raise e
