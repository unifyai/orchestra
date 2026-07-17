"""Exclusive sync leases backed by Postgres advisory locks + durable rows."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Project
from orchestra.db.scope import owner_key_for_context
from orchestra.web.api.sync_lease.schema import (
    SyncLeaseAcquireRequest,
    SyncLeaseReleaseRequest,
    SyncLeaseResponse,
)
from orchestra.web.api.utils.system_project import (
    is_system_project_name,
    require_system_project_owner,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SYNC_LEASE_CONTEXT = "_system/SyncLeases"
_UNIQUE_KEYS = {"lease_key": "str"}


def _check_project_write_permission(
    session,
    user_id: str,
    organization_id: int | None,
    project_id: int,
) -> None:
    project = session.get(Project, project_id)
    if project and is_system_project_name(project.name):
        require_system_project_owner(
            project,
            user_id=user_id,
            action="modified",
        )
    if organization_id is None:
        return
    has_permission = ResourceAccessDAO(session).check_user_permission(
        user_id,
        "project",
        project_id,
        "project:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to write to this project",
        )


def _parse_expires_at(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text_value = str(raw).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_project(
    *,
    session,
    user_id: str,
    organization_id: int | None,
    project_name: str,
):
    project_dao = ProjectDAO(
        session,
        OrganizationMemberDAO(session),
        ContextDAO(session),
    )
    try:
        project = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=user_id,
        )
    except (IndexError, AttributeError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found.",
        ) from exc
    _check_project_write_permission(
        session,
        user_id,
        organization_id,
        project.id,
    )
    return project


def _ensure_lease_context(session, project_id: int) -> int:
    context_dao = ContextDAO(session)
    return context_dao.get_or_create(
        project_id=project_id,
        name=SYNC_LEASE_CONTEXT,
        description="Exclusive leases for authoritative sync writers",
        is_versioned=False,
        allow_duplicates=False,
        unique_keys=_UNIQUE_KEYS,
    )


def _advisory_lock(session, *, project_id: int, lease_key: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"sync_lease:{project_id}:{lease_key}"},
    )


def _load_lease_row(
    session,
    *,
    project_id: int,
    context_id: int,
    lease_key: str,
):
    return session.execute(
        text(
            """
            SELECT le.id, le.data
            FROM log_event le
            JOIN log_event_context lec
              ON lec.log_event_id = le.id
             AND lec.project_id = le.project_id
            WHERE le.project_id = :project_id
              AND lec.context_id = :context_id
              AND le.data->>'lease_key' = :lease_key
            FOR UPDATE
            """,
        ),
        {
            "project_id": project_id,
            "context_id": context_id,
            "lease_key": lease_key,
        },
    ).fetchone()


@router.post(
    "/sync_lease/acquire",
    response_model=SyncLeaseResponse,
    responses={
        409: {
            "description": "Lease held by another writer",
        },
    },
)
def acquire_sync_lease(
    request_fastapi: Request,
    body: SyncLeaseAcquireRequest,
    session=Depends(get_db_session),
) -> SyncLeaseResponse:
    """Acquire an exclusive lease for an authoritative sync critical section.

    Acquire is race-safe: writers serialize on a Postgres advisory transaction
    lock keyed by ``(project, lease_key)``, then inspect/update a durable lease
    row. A held, unexpired lease owned by another holder returns HTTP 409.
    """
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project = _resolve_project(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
        project_name=body.project,
    )
    context_id = _ensure_lease_context(session, project.id)
    _advisory_lock(session, project_id=project.id, lease_key=body.lease_key)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=float(body.ttl_seconds))
    expires_at_iso = expires_at.isoformat()

    existing = _load_lease_row(
        session,
        project_id=project.id,
        context_id=context_id,
        lease_key=body.lease_key,
    )
    if existing is not None:
        data = dict(existing.data or {})
        current_holder = data.get("holder") or ""
        current_expires = _parse_expires_at(data.get("expires_at"))
        still_held = (
            bool(current_holder)
            and current_expires is not None
            and current_expires > now
            and current_holder != body.holder
        )
        if still_held:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Sync lease held by another writer",
                    "lease_key": body.lease_key,
                    "held_by": current_holder,
                    "expires_at": current_expires.isoformat(),
                },
            )
        session.execute(
            text(
                """
                UPDATE log_event
                SET data = CAST(:data AS jsonb),
                    updated_at = :now
                WHERE id = :log_id AND project_id = :project_id
                """,
            ),
            {
                "data": json.dumps(
                    {
                        "lease_key": body.lease_key,
                        "holder": body.holder,
                        "expires_at": expires_at_iso,
                    },
                ),
                "now": now,
                "log_id": existing.id,
                "project_id": project.id,
            },
        )
    else:
        result = session.execute(
            text(
                """
                INSERT INTO log_event (project_id, data, created_at, updated_at, owner_key)
                VALUES (
                    :project_id,
                    CAST(:data AS jsonb),
                    :now,
                    :now,
                    :owner_key
                )
                RETURNING id
                """,
            ),
            {
                "project_id": project.id,
                "data": json.dumps(
                    {
                        "lease_key": body.lease_key,
                        "holder": body.holder,
                        "expires_at": expires_at_iso,
                    },
                ),
                "now": now,
                "owner_key": owner_key_for_context(session, context_id),
            },
        ).fetchone()
        session.execute(
            text(
                """
                INSERT INTO log_event_context (project_id, log_event_id, context_id, owner_key)
                VALUES (
                    :project_id,
                    :log_id,
                    :context_id,
                    (SELECT owner_key FROM log_event
                     WHERE project_id = :project_id AND id = :log_id)
                )
                """,
            ),
            {
                "project_id": project.id,
                "log_id": result.id,
                "context_id": context_id,
            },
        )

    session.commit()
    return SyncLeaseResponse(
        acquired=True,
        lease_key=body.lease_key,
        holder=body.holder,
        expires_at=expires_at_iso,
    )


@router.post(
    "/sync_lease/release",
    response_model=SyncLeaseResponse,
)
def release_sync_lease(
    request_fastapi: Request,
    body: SyncLeaseReleaseRequest,
    session=Depends(get_db_session),
) -> SyncLeaseResponse:
    """Release a lease if (and only if) this holder still owns it."""
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project = _resolve_project(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
        project_name=body.project,
    )
    context_id = _ensure_lease_context(session, project.id)
    _advisory_lock(session, project_id=project.id, lease_key=body.lease_key)

    existing = _load_lease_row(
        session,
        project_id=project.id,
        context_id=context_id,
        lease_key=body.lease_key,
    )
    if existing is None:
        session.commit()
        return SyncLeaseResponse(
            released=True,
            lease_key=body.lease_key,
            holder=body.holder,
        )

    data = dict(existing.data or {})
    current_holder = data.get("holder") or ""
    if current_holder and current_holder != body.holder:
        session.commit()
        return SyncLeaseResponse(
            released=False,
            lease_key=body.lease_key,
            holder=body.holder,
            held_by=current_holder,
            expires_at=str(data.get("expires_at") or "") or None,
        )

    session.execute(
        text(
            """
            UPDATE log_event
            SET data = CAST(:data AS jsonb),
                updated_at = :now
            WHERE id = :log_id AND project_id = :project_id
            """,
        ),
        {
            "data": json.dumps(
                {
                    "lease_key": body.lease_key,
                    "holder": "",
                    "expires_at": "",
                },
            ),
            "now": datetime.now(timezone.utc),
            "log_id": existing.id,
            "project_id": project.id,
        },
    )
    session.commit()
    return SyncLeaseResponse(
        released=True,
        lease_key=body.lease_key,
        holder=body.holder,
    )
