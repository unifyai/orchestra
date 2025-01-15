from datetime import datetime, timezone
from typing import Dict, List

from fastapi import Depends
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
                filter_expr, _ = _substitute_placeholders(
                    dlog.equation,
                    transformed_logs,
                )
                filter_dict = str_filter_exp_to_dict(filter_expr)
                new_val = _compute_expression(filter_dict, LogEvent, session)[0][1]
                dlog.value = new_val
                dlog.updated_at = datetime.now(timezone.utc)

            session.commit()
        except Exception as e:
            session.rollback()
            raise e
