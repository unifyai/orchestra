"""DAO for external_write_intent outbox rows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from orchestra.db.models.core_models import ExternalWriteIntent

TERMINAL = {"confirmed", "failed"}


class ExternalWriteIntentDAO:
    def __init__(self, session):
        self.session = session

    def create(
        self,
        *,
        project_id: int,
        context_id: int,
        connector_id: str,
        binding: dict[str, Any],
        payload: dict[str, Any],
        idempotency_key: str,
        field_name: Optional[str] = None,
        log_event_ids: Optional[list[int]] = None,
    ) -> ExternalWriteIntent:
        existing = self.get_by_idempotency(
            project_id=project_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        row = ExternalWriteIntent(
            project_id=project_id,
            context_id=context_id,
            field_name=field_name,
            connector_id=connector_id,
            binding=binding,
            payload=payload,
            idempotency_key=idempotency_key,
            log_event_ids=list(log_event_ids or []),
            status="pending",
            attempts=0,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_by_idempotency(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> Optional[ExternalWriteIntent]:
        return (
            self.session.execute(
                select(ExternalWriteIntent).where(
                    ExternalWriteIntent.project_id == project_id,
                    ExternalWriteIntent.idempotency_key == idempotency_key,
                ),
            )
            .scalars()
            .first()
        )

    def get(self, intent_id: int) -> Optional[ExternalWriteIntent]:
        return self.session.get(ExternalWriteIntent, intent_id)

    def list_pending(self, *, limit: int = 50) -> list[ExternalWriteIntent]:
        return list(
            self.session.execute(
                select(ExternalWriteIntent)
                .where(ExternalWriteIntent.status == "pending")
                .order_by(ExternalWriteIntent.created_at.asc())
                .limit(limit),
            )
            .scalars()
            .all(),
        )

    def mark_in_progress(self, row: ExternalWriteIntent) -> None:
        row.status = "in_progress"
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.session.flush()

    def mark_confirmed(self, row: ExternalWriteIntent, result: Any) -> None:
        row.status = "confirmed"
        row.result = result
        row.last_error = None
        row.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.updated_at = row.confirmed_at
        self.session.flush()

    def mark_failed(self, row: ExternalWriteIntent, error: str) -> None:
        row.status = "failed"
        row.last_error = error[:4000]
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.session.flush()
