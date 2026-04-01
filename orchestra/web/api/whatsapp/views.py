"""Admin endpoints for WhatsApp pool routing.

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

from orchestra.db.dao.whatsapp_route_dao import WhatsAppRouteDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, OrganizationMember

admin_router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResolveResponse(BaseModel):
    assistant_id: int
    role: str  # "owner" | "contact"


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


class PoolNumberResponse(BaseModel):
    id: int
    number: str
    status: str
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
    """
    dao = WhatsAppRouteDAO(session)
    result = dao.resolve_inbound(pool_number, sender)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assistant found for this sender on this pool number.",
        )
    return ResolveResponse(**result)


@admin_router.post("/whatsapp/assign")
def assign_pool_number(
    body: AssignRequest,
    session: Session = Depends(get_db_session),
) -> AssignResponse:
    """Assign a WhatsApp pool number to an assistant.

    Determines all users who can access this assistant, checks for
    conflicts with their other assistants, and picks the first
    eligible pool number.
    """
    assistant = (
        session.query(Assistant).filter(Assistant.agent_id == body.assistant_id).first()
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # Determine accessible users
    accessible_user_ids = _get_accessible_user_ids(session, assistant)

    dao = WhatsAppRouteDAO(session)
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

    Called when an assistant sends a WhatsApp message to an external contact.
    Returns the pool number to use as the sender.
    """
    dao = WhatsAppRouteDAO(session)
    try:
        route = dao.get_or_create_route(body.assistant_id, body.contact_number)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    session.commit()

    pool = route.pool_number
    now = datetime.now(timezone.utc)
    # 5-minute safety margin accounts for clock skew between our
    # recording of last_inbound_at and WhatsApp's internal timer.
    window_open = route.last_inbound_at is not None and (
        now - route.last_inbound_at
    ) < timedelta(hours=23, minutes=55)
    return RouteResponse(
        pool_number=pool.number,
        contact_number=route.contact_number,
        assistant_id=route.assistant_id,
        window_open=window_open,
    )


@admin_router.delete("/whatsapp/routes")
def delete_routes(
    assistant_id: int = Query(
        ...,
        description="Delete all routes for this assistant.",
    ),
    session: Session = Depends(get_db_session),
):
    """Bulk-delete all WhatsApp routes for an assistant.

    Called when WhatsApp is disabled or the assistant is deleted.
    """
    dao = WhatsAppRouteDAO(session)
    count = dao.delete_routes_for_assistant(assistant_id)
    session.commit()
    return {"deleted": count}


@admin_router.get("/whatsapp/pool")
def get_pool_status(
    session: Session = Depends(get_db_session),
) -> list[PoolNumberResponse]:
    """Return the current state of the WhatsApp number pool."""
    dao = WhatsAppRouteDAO(session)
    numbers = dao.list_pool_numbers()
    return [
        PoolNumberResponse(
            id=n.id,
            number=n.number,
            status=n.status,
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
    dao = WhatsAppRouteDAO(session)
    try:
        pool = dao.add_pool_number(body.number, body.twilio_sender_sid)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    session.commit()
    return PoolNumberResponse(
        id=pool.id,
        number=pool.number,
        status=pool.status,
        twilio_sender_sid=pool.twilio_sender_sid,
    )


@admin_router.patch("/whatsapp/pool/{pool_id}")
def update_pool_number(
    pool_id: int,
    body: PoolNumberUpdateRequest,
    session: Session = Depends(get_db_session),
) -> PoolNumberResponse:
    """Update a pool number's status or Twilio sender SID."""
    dao = WhatsAppRouteDAO(session)
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
        twilio_sender_sid=pool.twilio_sender_sid,
    )


@admin_router.delete("/whatsapp/pool/{pool_id}")
def delete_pool_number(
    pool_id: int,
    session: Session = Depends(get_db_session),
):
    """Remove a pool number (only if no active contacts use it)."""
    dao = WhatsAppRouteDAO(session)
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
