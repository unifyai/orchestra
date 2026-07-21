"""Enqueue and drain external write intents (through-write outbox)."""

from __future__ import annotations

from typing import Any, Optional

from orchestra.db.dao.external_field_binding_dao import ExternalFieldBindingDAO
from orchestra.db.dao.external_write_intent_dao import ExternalWriteIntentDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.external_bindings.auth import resolve_auth
from orchestra.external_bindings.planner import sidecar_key
from orchestra.external_bindings.registry import get_connector


def enqueue_external_write(
    session,
    *,
    project_id: int,
    context_id: int,
    connector_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    binding: Optional[dict[str, Any]] = None,
    field_name: Optional[str] = None,
    log_event_ids: Optional[list[int]] = None,
    deliver: str = "async",
) -> dict[str, Any]:
    """Create an outbox intent; optionally deliver synchronously.

    ``deliver``:
      - ``async`` — persist ``pending`` for later drain
      - ``sync`` — deliver in-process before returning
    """
    binding_body = dict(binding or {})
    if field_name and not binding_body:
        row = ExternalFieldBindingDAO(session).get(
            project_id=project_id,
            context_id=context_id,
            field_name=field_name,
        )
        if row is None:
            raise ValueError(f"No external binding for field '{field_name}'")
        connector_id = row.connector_id
        binding_body = dict(row.binding or {})
    binding_body.setdefault("connector_id", connector_id)
    get_connector(connector_id)  # validate early

    dao = ExternalWriteIntentDAO(session)
    intent = dao.create(
        project_id=project_id,
        context_id=context_id,
        connector_id=connector_id,
        binding=binding_body,
        payload=payload,
        idempotency_key=idempotency_key,
        field_name=field_name,
        log_event_ids=log_event_ids,
    )
    session.commit()

    if deliver == "sync" and intent.status == "pending":
        deliver_intent(session, intent.id)
        intent = dao.get(intent.id)

    return _intent_public(intent)


def drain_external_writes(
    session,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Deliver pending intents. Safe to call from admin cron / worker."""
    dao = ExternalWriteIntentDAO(session)
    pending = dao.list_pending(limit=limit)
    results = []
    for intent in pending:
        results.append(deliver_intent(session, intent.id))
    return {
        "drained": len(results),
        "results": results,
    }


def deliver_intent(session, intent_id: int) -> dict[str, Any]:
    dao = ExternalWriteIntentDAO(session)
    intent = dao.get(intent_id)
    if intent is None:
        raise ValueError(f"Intent {intent_id} not found")
    if intent.status == "confirmed":
        return _intent_public(intent)
    if intent.status == "in_progress":
        # Allow retry of stuck in_progress by re-entering delivery.
        pass

    dao.mark_in_progress(intent)
    session.commit()

    connector = get_connector(intent.connector_id)
    binding_body = dict(intent.binding or {})
    try:
        auth = resolve_auth(
            binding_body,
            session=session,
            project_id=intent.project_id,
            context_id=intent.context_id,
            require=bool(binding_body.get("auth_secret_ref")),
        )
    except ValueError as exc:
        dao.mark_failed(intent, str(exc))
        session.commit()
        return _intent_public(intent)
    if not hasattr(connector, "execute_write"):
        dao.mark_failed(intent, "connector does not support execute_write")
        session.commit()
        return _intent_public(intent)

    result = connector.execute_write(
        binding=binding_body,
        payload=dict(intent.payload or {}),
        idempotency_key=intent.idempotency_key,
        auth=auth,
    )
    if not result.ok:
        dao.mark_failed(intent, result.error or "write failed")
        session.commit()
        return _intent_public(intent)

    dao.mark_confirmed(
        intent,
        {
            "response": result.response,
            "external_token": result.external_token,
        },
    )
    _invalidate_hydrate_cache(
        session,
        project_id=intent.project_id,
        field_name=intent.field_name,
        log_event_ids=list(intent.log_event_ids or []),
    )
    session.commit()
    return _intent_public(intent)


def _invalidate_hydrate_cache(
    session,
    *,
    project_id: int,
    field_name: Optional[str],
    log_event_ids: list[int],
) -> None:
    """Drop sidecars so the next read re-hydrates after a successful write."""
    if not field_name or not log_event_ids:
        return
    ops = [
        {
            "log_event_id": int(lid),
            "key": sidecar_key(field_name),
            "value": None,
        }
        for lid in log_event_ids
    ]
    LogEventDAO(session).bulk_merge_data(ops, project_id=project_id)


def _intent_public(intent) -> dict[str, Any]:
    return {
        "id": intent.id,
        "status": intent.status,
        "connector_id": intent.connector_id,
        "field_name": intent.field_name,
        "idempotency_key": intent.idempotency_key,
        "attempts": intent.attempts,
        "last_error": intent.last_error,
        "result": intent.result,
        "log_event_ids": list(intent.log_event_ids or []),
        "created_at": intent.created_at.isoformat() if intent.created_at else None,
        "confirmed_at": (
            intent.confirmed_at.isoformat() if intent.confirmed_at else None
        ),
    }
