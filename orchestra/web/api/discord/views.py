"""Admin endpoints for Discord shared-pool routing.

Mirrors the WhatsApp admin endpoints but parameterized for Discord DM bots.
Called by the Communication service (Discord Gateway) for inbound routing
and by Orchestra's own assistant-contact provisioning flow.
"""

import logging
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
    role: Optional[str] = None
    action: Optional[str] = None


class AssignRequest(BaseModel):
    assistant_id: int = Field(..., description="The assistant to enable Discord for.")


class AssignResponse(BaseModel):
    pool_bot_id: str = Field(..., description="The assigned pool bot ID.")
    assistant_id: int


class RouteRequest(BaseModel):
    assistant_id: int
    contact_number: str = Field(
        ...,
        description="External contact's Discord user ID (snowflake).",
    )


class RouteResponse(BaseModel):
    pool_bot_id: str
    contact_number: str
    assistant_id: int
    conflict_resolved: bool = Field(
        False,
        description="True if a conflict was detected and resolved inline.",
    )
    conflict_event_id: Optional[int] = Field(
        None,
        description="ID of the ConflictEvent if a conflict was resolved.",
    )


class PoolBotResponse(BaseModel):
    id: int
    bot_id: str
    status: str
    platform: str = "discord"


class PoolBotCreateRequest(BaseModel):
    bot_id: str = Field(
        ...,
        description="Discord bot application ID to add to the pool.",
    )


class PoolBotUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, description="'active' or 'inactive'.")


class NotificationStatusRequest(BaseModel):
    conflict_event_id: int
    recipient_id: str
    message_id: str
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


PLATFORM = "discord"


def _dao(session: Session) -> SharedPoolDAO:
    return SharedPoolDAO(session, PLATFORM)


@admin_router.get("/discord/resolve")
def resolve_inbound(
    bot_id: str = Query(..., description="The pool bot ID that received the DM."),
    sender: str = Query(..., description="The sender's Discord user ID (snowflake)."),
    session: Session = Depends(get_db_session),
) -> ResolveResponse:
    """Resolve an inbound Discord DM to an assistant.

    Called by the Communication service when a Discord DM arrives via Gateway.
    """
    dao = _dao(session)
    result = dao.resolve_inbound(bot_id, sender)

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assistant found for this sender on this pool bot.",
        )

    if "action" in result:
        return ResolveResponse(action=result["action"])

    return ResolveResponse(
        assistant_id=result["assistant_id"],
        role=result["role"],
    )


@admin_router.post("/discord/assign")
def assign_pool_bot(
    body: AssignRequest,
    session: Session = Depends(get_db_session),
) -> AssignResponse:
    """Assign a Discord pool bot to an assistant."""
    assistant = (
        session.query(Assistant).filter(Assistant.agent_id == body.assistant_id).first()
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    accessible_user_ids = _get_accessible_user_ids(session, assistant)

    dao = _dao(session)
    try:
        pool = dao.assign_pool_number(body.assistant_id, accessible_user_ids)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    return AssignResponse(
        pool_bot_id=pool.number,
        assistant_id=body.assistant_id,
    )


@admin_router.post("/discord/route")
def create_route(
    body: RouteRequest,
    session: Session = Depends(get_db_session),
) -> RouteResponse:
    """Create or retrieve a route for an outbound Discord DM.

    If a conflict is detected (another assistant on the same pool bot
    already routes to this contact), the conflict is resolved inline.
    """
    dao = _dao(session)
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

    return RouteResponse(
        pool_bot_id=pool.number,
        contact_number=route.contact_number,
        assistant_id=route.assistant_id,
        conflict_resolved=resolution is not None,
        conflict_event_id=resolution.conflict_event_id if resolution else None,
    )


@admin_router.delete("/discord/routes")
def delete_routes(
    assistant_id: int = Query(
        ...,
        description="Delete all routes for this assistant.",
    ),
    session: Session = Depends(get_db_session),
):
    """Bulk-delete all Discord routes for an assistant."""
    dao = _dao(session)
    count = dao.delete_routes_for_assistant(assistant_id)
    session.commit()
    return {"deleted": count}


@admin_router.get("/discord/pool")
def get_pool_status(
    session: Session = Depends(get_db_session),
) -> list[PoolBotResponse]:
    """Return the current state of the Discord bot pool."""
    dao = _dao(session)
    bots = dao.list_pool_numbers()
    return [
        PoolBotResponse(
            id=b.id,
            bot_id=b.number,
            status=b.status,
            platform=b.platform,
        )
        for b in bots
    ]


@admin_router.post("/discord/pool")
def add_pool_bot(
    body: PoolBotCreateRequest,
    session: Session = Depends(get_db_session),
) -> PoolBotResponse:
    """Add a new Discord bot to the pool."""
    dao = _dao(session)
    try:
        pool = dao.add_pool_number(body.bot_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    session.commit()
    return PoolBotResponse(
        id=pool.id,
        bot_id=pool.number,
        status=pool.status,
        platform=pool.platform,
    )


@admin_router.patch("/discord/pool/{pool_id}")
def update_pool_bot(
    pool_id: int,
    body: PoolBotUpdateRequest,
    session: Session = Depends(get_db_session),
) -> PoolBotResponse:
    """Update a pool bot's status."""
    dao = _dao(session)
    kwargs: dict = {}
    if body.status is not None:
        kwargs["status"] = body.status
    try:
        pool = dao.update_pool_number(pool_id, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    session.commit()
    return PoolBotResponse(
        id=pool.id,
        bot_id=pool.number,
        status=pool.status,
        platform=pool.platform,
    )


@admin_router.delete("/discord/pool/{pool_id}")
def delete_pool_bot(
    pool_id: int,
    session: Session = Depends(get_db_session),
):
    """Remove a pool bot (only if no active contacts use it)."""
    dao = _dao(session)
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


@admin_router.post("/discord/notification-status")
def update_notification_status(
    body: NotificationStatusRequest,
    session: Session = Depends(get_db_session),
):
    """Receive delivery status updates for conflict notifications."""
    dao = _dao(session)
    event = dao.update_notification_status(
        body.conflict_event_id,
        body.recipient_id,
        body.message_id,
        body.status,
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conflict event not found.",
        )
    session.commit()
    return {"conflict_event_id": event.id, "status": event.status}


@admin_router.get("/discord/conflicts")
def list_conflict_events(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status: notifying, resolved, notification_failed, failed",
    ),
    limit: int = Query(50, le=200),
    session: Session = Depends(get_db_session),
) -> list[ConflictEventResponse]:
    """List Discord conflict events for monitoring and debugging."""
    from orchestra.db.models.orchestra_models import ConflictEvent

    query = session.query(ConflictEvent).filter(ConflictEvent.platform == PLATFORM)
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
