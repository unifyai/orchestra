"""Admin endpoints for shared-pool routing.

These endpoints are called by the Communication service (adapters) for
inbound routing and by Orchestra's own assistant-contact provisioning
flow for pool assignment and outbound route creation.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, OrganizationMember

admin_router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResolveResponse(BaseModel):
    assistant_id: Optional[int] = None
    role: Optional[str] = None  # "owner" | "contact"
    action: Optional[str] = None  # "auto_reply" | "reject_cold"


class AssignRequest(BaseModel):
    assistant_id: int = Field(..., description="The assistant to enable WhatsApp for.")


class AssignResponse(BaseModel):
    pool_number: str = Field(..., description="The assigned pool number (E.164).")
    assistant_id: int


class RouteRequest(BaseModel):
    assistant_id: int
    contact_number: str = Field(
        ...,
        description="External contact's WhatsApp number (E.164).",
    )


class RouteResponse(BaseModel):
    pool_number: str
    contact_number: str
    assistant_id: int
    window_open: bool = Field(
        ...,
        description="True if the contact messaged within the last 24h (free-form allowed).",
    )
    conflict_resolved: bool = Field(
        False,
        description="True if a conflict was detected and resolved inline.",
    )
    conflict_event_id: Optional[int] = Field(
        None,
        description="ID of the ConflictEvent if a conflict was resolved.",
    )


class PoolNumberResponse(BaseModel):
    id: int
    number: str
    status: str
    platform: str = "whatsapp"
    twilio_sender_sid: Optional[str] = None


class PoolNumberCreateRequest(BaseModel):
    number: str = Field(..., description="E.164 WhatsApp number to add to the pool.")
    twilio_sender_sid: Optional[str] = Field(
        None,
        description="Twilio Messaging Service SID for this number.",
    )


class PoolNumberUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, description="'active' or 'inactive'.")
    twilio_sender_sid: Optional[str] = Field(
        None,
        description="Twilio Messaging Service SID (send null to clear).",
    )


class NotificationStatusRequest(BaseModel):
    conflict_event_id: int
    recipient_number: str
    message_sid: str
    status: str


class ConflictEventResponse(BaseModel):
    id: int
    platform: str
    conflict_type: str
    trigger_assistant_id: Optional[int]
    affected_assistant_ids: list
    old_pool_assignments: dict
    new_pool_assignments: dict
    notification_recipients: Optional[list]
    notification_status: Optional[dict]
    status: str
    created_at: Optional[str]
    resolved_at: Optional[str]


class CallPermissionUpdateRequest(BaseModel):
    pool_number: str = Field(..., description="Pool number (E.164).")
    contact_number: str = Field(..., description="External contact number (E.164).")
    status: str = Field(
        ...,
        description="Permission status: 'accepted' or 'rejected'.",
        pattern="^(accepted|rejected)$",
    )


class CallPermissionResponse(BaseModel):
    permitted: bool
    expires_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@admin_router.get("/whatsapp/resolve")
def resolve_inbound(
    pool_number: str = Query(..., description="The To number (pool number, E.164)."),
    sender: str = Query(..., description="The From number (sender, E.164)."),
    session: Session = Depends(get_db_session),
) -> ResolveResponse:
    """Resolve an inbound WhatsApp message to an assistant.

    Called by the Communication adapters when a WhatsApp message arrives.
    Returns assistant routing info, or an action directive for auto-reply
    / cold-message rejection.
    """
    dao = SharedPoolDAO(session)
    result = dao.resolve_inbound(pool_number, sender)

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assistant found for this sender on this pool number.",
        )

    if "action" in result:
        return ResolveResponse(action=result["action"])

    return ResolveResponse(
        assistant_id=result["assistant_id"],
        role=result["role"],
    )


@admin_router.post("/whatsapp/assign")
def assign_pool_number(
    body: AssignRequest,
    session: Session = Depends(get_db_session),
) -> AssignResponse:
    """Assign a WhatsApp pool number to an assistant."""
    assistant = (
        session.query(Assistant).filter(Assistant.agent_id == body.assistant_id).first()
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    accessible_user_ids = _get_accessible_user_ids(session, assistant)

    dao = SharedPoolDAO(session)
    try:
        pool = dao.assign_pool_number(body.assistant_id, accessible_user_ids)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    return AssignResponse(
        pool_number=pool.number,
        assistant_id=body.assistant_id,
    )


@admin_router.post("/whatsapp/route")
def create_route(
    body: RouteRequest,
    session: Session = Depends(get_db_session),
) -> RouteResponse:
    """Create or retrieve a route for an outbound WhatsApp message.

    If a conflict is detected (another assistant on the same pool number
    already routes to this contact), the conflict is resolved inline:
    the initiating assistant is reassigned to a new pool number.
    """
    dao = SharedPoolDAO(session)
    try:
        route, resolution = dao.get_or_create_route(
            body.assistant_id,
            body.contact_number,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    session.commit()

    pool = route.pool_number
    now = datetime.now(timezone.utc)
    window_open = route.last_inbound_at is not None and (
        now - route.last_inbound_at
    ) < timedelta(hours=23, minutes=55)

    return RouteResponse(
        pool_number=pool.number,
        contact_number=route.contact_number,
        assistant_id=route.assistant_id,
        window_open=window_open,
        conflict_resolved=resolution is not None,
        conflict_event_id=resolution.conflict_event_id if resolution else None,
    )


@admin_router.delete("/whatsapp/routes")
def delete_routes(
    assistant_id: int = Query(
        ...,
        description="Delete all routes for this assistant.",
    ),
    session: Session = Depends(get_db_session),
):
    """Bulk-delete all WhatsApp routes for an assistant."""
    dao = SharedPoolDAO(session)
    count = dao.delete_routes_for_assistant(assistant_id)
    session.commit()
    return {"deleted": count}


@admin_router.get("/whatsapp/pool")
def get_pool_status(
    session: Session = Depends(get_db_session),
) -> list[PoolNumberResponse]:
    """Return the current state of the WhatsApp number pool."""
    dao = SharedPoolDAO(session)
    numbers = dao.list_pool_numbers()
    return [
        PoolNumberResponse(
            id=n.id,
            number=n.number,
            status=n.status,
            platform=n.platform,
            twilio_sender_sid=n.twilio_sender_sid,
        )
        for n in numbers
    ]


@admin_router.post("/whatsapp/pool")
def add_pool_number(
    body: PoolNumberCreateRequest,
    session: Session = Depends(get_db_session),
) -> PoolNumberResponse:
    """Add a new WhatsApp number to the pool."""
    dao = SharedPoolDAO(session)
    try:
        pool = dao.add_pool_number(body.number, body.twilio_sender_sid)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    session.commit()
    return PoolNumberResponse(
        id=pool.id,
        number=pool.number,
        status=pool.status,
        platform=pool.platform,
        twilio_sender_sid=pool.twilio_sender_sid,
    )


@admin_router.patch("/whatsapp/pool/{pool_id}")
def update_pool_number(
    pool_id: int,
    body: PoolNumberUpdateRequest,
    session: Session = Depends(get_db_session),
) -> PoolNumberResponse:
    """Update a pool number's status or Twilio sender SID."""
    dao = SharedPoolDAO(session)
    kwargs: dict = {}
    if body.status is not None:
        kwargs["status"] = body.status
    if body.twilio_sender_sid is not None:
        kwargs["twilio_sender_sid"] = body.twilio_sender_sid
    try:
        pool = dao.update_pool_number(pool_id, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    session.commit()
    return PoolNumberResponse(
        id=pool.id,
        number=pool.number,
        status=pool.status,
        platform=pool.platform,
        twilio_sender_sid=pool.twilio_sender_sid,
    )


@admin_router.delete("/whatsapp/pool/{pool_id}")
def delete_pool_number(
    pool_id: int,
    session: Session = Depends(get_db_session),
):
    """Remove a pool number (only if no active contacts use it)."""
    dao = SharedPoolDAO(session)
    try:
        route_count = dao.delete_pool_number(pool_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    session.commit()
    return {"deleted_routes": route_count}


# ---------------------------------------------------------------------------
# Conflict event endpoints
# ---------------------------------------------------------------------------


@admin_router.post("/whatsapp/notification-status")
def update_notification_status(
    body: NotificationStatusRequest,
    session: Session = Depends(get_db_session),
):
    """Receive delivery status updates for conflict notifications.

    Called by Communication when Twilio sends a status callback for a
    notification message sent during conflict resolution.
    """
    dao = SharedPoolDAO(session)
    event = dao.update_notification_status(
        body.conflict_event_id,
        body.recipient_number,
        body.message_sid,
        body.status,
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conflict event not found.",
        )
    session.commit()
    return {"conflict_event_id": event.id, "status": event.status}


@admin_router.get("/whatsapp/conflicts")
def list_conflict_events(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status: notifying, resolved, notification_failed, failed",
    ),
    limit: int = Query(50, le=200),
    session: Session = Depends(get_db_session),
) -> list[ConflictEventResponse]:
    """List conflict events for monitoring and debugging."""
    from orchestra.db.models.orchestra_models import ConflictEvent

    query = session.query(ConflictEvent).filter(ConflictEvent.platform == "whatsapp")
    if status_filter:
        query = query.filter(ConflictEvent.status == status_filter)
    events = query.order_by(ConflictEvent.created_at.desc()).limit(limit).all()

    return [
        ConflictEventResponse(
            id=e.id,
            platform=e.platform,
            conflict_type=e.conflict_type,
            trigger_assistant_id=e.trigger_assistant_id,
            affected_assistant_ids=e.affected_assistant_ids,
            old_pool_assignments=e.old_pool_assignments,
            new_pool_assignments=e.new_pool_assignments,
            notification_recipients=e.notification_recipients,
            notification_status=e.notification_status,
            status=e.status,
            created_at=e.created_at.isoformat() if e.created_at else None,
            resolved_at=e.resolved_at.isoformat() if e.resolved_at else None,
        )
        for e in events
    ]


# ---------------------------------------------------------------------------
# Call permission endpoints
# ---------------------------------------------------------------------------


@admin_router.post("/whatsapp/call-permission")
def update_call_permission(
    body: CallPermissionUpdateRequest,
    session: Session = Depends(get_db_session),
):
    """Store a call permission response from a contact.

    Called by Communication when a contact accepts or rejects a voice
    call permission template.
    """
    dao = SharedPoolDAO(session)
    route = dao.update_call_permission(
        body.pool_number,
        body.contact_number,
        body.status,
    )
    if route is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No route found for this pool/contact pair.",
        )
    session.commit()
    return {
        "pool_number": body.pool_number,
        "contact_number": body.contact_number,
        "status": body.status,
        "expires_at": (
            route.call_permission_expires_at.isoformat()
            if route.call_permission_expires_at
            else None
        ),
    }


@admin_router.get("/whatsapp/call-permission")
def check_call_permission(
    pool_number: str = Query(..., description="Pool number (E.164)."),
    contact_number: str = Query(..., description="Contact number (E.164)."),
    session: Session = Depends(get_db_session),
) -> CallPermissionResponse:
    """Check whether a contact has granted voice call permission.

    Called by Communication before placing an outbound WhatsApp call
    to decide between direct call vs. invite template.
    """
    dao = SharedPoolDAO(session)
    permitted, expires_at = dao.check_call_permission(pool_number, contact_number)
    return CallPermissionResponse(
        permitted=permitted,
        expires_at=expires_at.isoformat() if expires_at else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_accessible_user_ids(session: Session, assistant: Assistant) -> list[str]:
    """Determine all user IDs who can access the given assistant."""
    user_ids = [assistant.user_id]

    if assistant.organization_id is not None:
        members = (
            session.query(OrganizationMember.user_id)
            .filter(
                OrganizationMember.organization_id == assistant.organization_id,
            )
            .all()
        )
        for (uid,) in members:
            if uid not in user_ids:
                user_ids.append(uid)

    return user_ids
