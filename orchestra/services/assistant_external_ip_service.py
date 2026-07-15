"""Assistant-owned external IP lifecycle intent helpers.

Cloud reconciliation is deliberately outside this service.  These helpers
only persist the desired allocation lifecycle for deployment to consume.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantExternalIP,
    AssistantExternalIPHistory,
)
from orchestra.web.api.utils import assistant_infra
from orchestra.web.api.utils.http_client import get_async_client

logger = logging.getLogger(__name__)

ASSISTANT_STATIC_IP_RECONCILE_PATH = "/infra/vm/assistant-static-ip/reconcile"
ASSISTANT_STATIC_IP_RELEASE_PATH = "/infra/vm/assistant-static-ip/{assistant_id}"


def ensure_pending_assistant_external_ip(
    session: Session,
    *,
    assistant: Assistant,
) -> AssistantExternalIP:
    """Create or re-queue the assistant's persistent external-IP resource."""
    external_ip = (
        session.query(AssistantExternalIP)
        .filter(AssistantExternalIP.assistant_id == assistant.agent_id)
        .one_or_none()
    )
    if external_ip is None:
        external_ip = AssistantExternalIP(
            assistant_id=assistant.agent_id,
            state="pending",
            active_operation="reserve",
        )
        session.add(external_ip)
        session.flush()
        record_assistant_external_ip_history(
            session,
            external_ip,
            operation="requested",
        )
    elif external_ip.state == "retained":
        external_ip.state = "pending"
        external_ip.active_operation = "reserve"
        external_ip.retained_at = None
        record_assistant_external_ip_history(session, external_ip, operation="reused")

    return external_ip


def retain_assistant_external_ip(
    session: Session,
    *,
    assistant: Assistant,
) -> AssistantExternalIP | None:
    """Retain an existing address request when managed desktop is disabled."""
    external_ip = (
        session.query(AssistantExternalIP)
        .filter(AssistantExternalIP.assistant_id == assistant.agent_id)
        .one_or_none()
    )
    if external_ip is None or external_ip.state == "retained":
        return external_ip

    external_ip.state = "retained"
    external_ip.active_operation = None
    external_ip.retained_at = _dt.datetime.now(_dt.timezone.utc)
    record_assistant_external_ip_history(session, external_ip, operation="retained")
    return external_ip


async def reconcile_assistant_external_ip(
    session_factory: Callable[[], Session],
    *,
    assistant_id: int,
) -> None:
    """Best-effort reserve of an assistant-owned address through deploy."""
    comms_url = assistant_infra._comms_url()
    admin_key = assistant_infra.ADMIN_KEY
    if not comms_url or not admin_key:
        logger.info(
            "Skipping assistant external-IP reconcile for %s: comms is not configured",
            assistant_id,
        )
        return

    try:
        response = await get_async_client().post(
            f"{comms_url}{ASSISTANT_STATIC_IP_RECONCILE_PATH}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"assistant_id": str(assistant_id)},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("deploy returned a non-object static-IP response")
    except Exception as exc:
        _record_reconcile_error(
            session_factory, assistant_id=assistant_id, error=str(exc)
        )
        logger.warning(
            "Assistant external-IP reconcile failed for %s: %s", assistant_id, exc
        )
        return

    with session_factory() as session:
        external_ip = _get_external_ip(session, assistant_id)
        # A concurrent disable retains the allocation and must win over a late
        # reconcile response.
        if external_ip is None or external_ip.state == "retained":
            return
        external_ip.gcp_address_name = _optional_string(payload.get("name"))
        external_ip.address = _optional_string(payload.get("address"))
        external_ip.region = _optional_string(payload.get("region"))
        external_ip.state = "reserved"
        external_ip.active_operation = None
        record_assistant_external_ip_history(
            session,
            external_ip,
            operation="reserved",
            details={
                "created": bool(payload.get("created", False)),
                "deploy_status": _optional_string(payload.get("status")),
            },
        )
        session.commit()


async def release_assistant_external_ip(
    session: Session | None,
    *,
    assistant_id: int,
) -> dict[str, Any]:
    """Best-effort idempotent release through deploy, with lifecycle recording.

    The durable assistant-cleanup worker calls this after assistant deletion.
    Its database row has then been cascaded away, but the cleanup task retains
    the result and retries the idempotent deploy endpoint when needed.
    """
    comms_url = assistant_infra._comms_url()
    admin_key = assistant_infra.ADMIN_KEY
    if not comms_url or not admin_key:
        return {
            "success": True,
            "skipped": True,
            "reason": "missing_comms_config",
            "errors": [],
        }

    external_ip = (
        _get_external_ip(session, assistant_id) if session is not None else None
    )
    if external_ip is not None:
        external_ip.state = "releasing"
        external_ip.active_operation = "release"
        record_assistant_external_ip_history(
            session, external_ip, operation="releasing"
        )

    try:
        response = await get_async_client().delete(
            f"{comms_url}{ASSISTANT_STATIC_IP_RELEASE_PATH.format(assistant_id=assistant_id)}",
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("deploy returned a non-object static-IP release response")
    except Exception as exc:
        if external_ip is not None:
            external_ip.state = "error"
            external_ip.active_operation = None
            record_assistant_external_ip_history(
                session,
                external_ip,
                operation="release_failed",
                details={"error": str(exc)},
            )
        return {"success": False, "errors": [str(exc)]}

    if external_ip is not None:
        external_ip.state = "released"
        external_ip.active_operation = None
        record_assistant_external_ip_history(
            session,
            external_ip,
            operation="released",
            details={"released": bool(payload.get("released", False))},
        )
    return {"success": True, "response": payload, "errors": []}


def record_assistant_external_ip_history(
    session: Session,
    external_ip: AssistantExternalIP,
    *,
    operation: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an immutable audit entry for a resource lifecycle transition."""
    session.add(
        AssistantExternalIPHistory(
            external_ip_id=external_ip.id,
            assistant_id=external_ip.assistant_id,
            state=external_ip.state,
            operation=operation,
            details=details,
        ),
    )


def _record_reconcile_error(
    session_factory: Callable[[], Session],
    *,
    assistant_id: int,
    error: str,
) -> None:
    with session_factory() as session:
        external_ip = _get_external_ip(session, assistant_id)
        if external_ip is None or external_ip.state == "retained":
            return
        external_ip.state = "error"
        external_ip.active_operation = None
        record_assistant_external_ip_history(
            session,
            external_ip,
            operation="reconcile_failed",
            details={"error": error},
        )
        session.commit()


def _get_external_ip(
    session: Session,
    assistant_id: int,
) -> AssistantExternalIP | None:
    return (
        session.query(AssistantExternalIP)
        .filter(AssistantExternalIP.assistant_id == assistant_id)
        .one_or_none()
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
