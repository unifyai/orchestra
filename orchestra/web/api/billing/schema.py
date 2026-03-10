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


class AutoRechargeResponse(BaseModel):
    """
    Combined auto-recharge settings and eligibility.

    Returned by ``GET /billing/auto-recharge``.
    """

    # Current settings
    enabled: bool = False
    threshold: float = 0.0
    qty: float = 25.0

    # Eligibility (fraud-prevention spending gate)
    eligible: bool = False
    total_spending: float = 0.0
    minimum_spend_required: float = 0.0
    remaining_spend_needed: float = 0.0


class AutoRechargeUpdateRequest(BaseModel):
    """
    Request body for ``PUT /billing/auto-recharge``.

    Only ``enabled`` is required.  ``threshold`` and ``qty`` are optional
    so callers can toggle the feature on/off without re-sending the amounts.
    """

    enabled: bool
    threshold: Optional[float] = None
    qty: Optional[float] = None
