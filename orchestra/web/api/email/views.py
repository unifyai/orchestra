"""Admin endpoints for shared email routing."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.services.shared_coordinator_routing import (
    resolve_shared_coordinator_owner,
)
from orchestra.services.universal_unity_email import is_universal_unity_email_address

admin_router = APIRouter()


class ResolveResponse(BaseModel):
    assistant_id: Optional[int] = None
    role: Optional[str] = None
    action: Optional[str] = None


@admin_router.get("/email/resolve")
def resolve_inbound(
    mailbox: str = Query(..., description="The shared mailbox address."),
    sender: str = Query(..., description="The inbound sender email address."),
    session: Session = Depends(get_db_session),
) -> ResolveResponse:
    """Resolve an inbound message on the shared coordinator mailbox."""
    mailbox_normalized = mailbox.strip().lower()
    sender_normalized = sender.strip().lower()
    if not is_universal_unity_email_address(mailbox_normalized):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No shared email routing configured for this mailbox.",
        )

    result = resolve_shared_coordinator_owner(
        session,
        platform="email",
        contact_type="email",
        contact_value=mailbox_normalized,
        sender=sender_normalized,
    )
    if "action" in result:
        return ResolveResponse(action=result["action"])
    return ResolveResponse(
        assistant_id=result["assistant_id"],
        role=result["role"],
    )
