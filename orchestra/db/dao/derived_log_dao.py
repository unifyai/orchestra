from datetime import datetime, timezone
from typing import Dict

from fastapi import Depends
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import DerivedLog


class OverwriteError(Exception):
    pass


# noinspection PyBroadException
class DerivedLogDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create(
        self,
        log_event_id: int,
        equation: str,
        referenced_logs: Dict[str, int],
    ) -> int:

        value = 0
        inferred_type = str

        ts = datetime.now(timezone.utc)

        new_derived_log = DerivedLog(
            log_event_id=log_event_id,
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
