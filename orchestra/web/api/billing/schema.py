"""Pydantic schemas for billing endpoints."""

from typing import Optional

from pydantic import BaseModel


class CheckoutSessionResponse(BaseModel):
    """Response from the checkout-session endpoint."""

    url: str
    session_id: str


class PortalSessionResponse(BaseModel):
    """Response from the portal-session endpoint."""

    url: str


class CheckoutStatusResponse(BaseModel):
    """Response from the checkout-status endpoint."""

    status: Optional[str] = None
    payment_status: Optional[str] = None
