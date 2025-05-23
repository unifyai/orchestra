"""Stripe invoice-webhook handler (import-safe).

The module exposes:
• process_invoice_event(event, session)  – core logic (pure, test-friendly)
• handle_event(event)                    – convenience wrapper that opens
                                           its own DB session
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.models.orchestra_models import WebhookLog
from orchestra.db.session import SessionLocal  # ← must exist for tests
from orchestra.observability.metrics import invoice_failed_total, invoice_paid_total

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
def process_invoice_event(event: Dict, session: Session) -> Response:  # noqa: D401
    """Business logic for *invoice.* events coming from Stripe webhooks."""
    data = event["data"]["object"]
    invoice_id: str = data["id"]
    event_id: str = event["id"]

    # idempotency guard -----------------------------------------------------
    if session.query(WebhookLog).filter_by(event_id=event_id).first():
        return Response(status_code=200)

    session.add(
        WebhookLog(
            id=str(uuid.uuid4()),
            event_id=event_id,
            event_type=event["type"],
        ),
    )
    session.flush()

    user_ids_subq = (
        select(Recharge.user_id)
        .where(Recharge.stripe_invoice_id == invoice_id)
        .scalar_subquery()
    )

    # success ---------------------------------------------------------------
    if event["type"] == "invoice.payment_succeeded":
        (
            session.query(Recharge)
            .filter_by(stripe_invoice_id=invoice_id)
            .update({"status": RechargeStatus.PAID}, synchronize_session=False)
        )
        (
            session.query(User)
            .filter(User.id.in_(user_ids_subq))
            .update({"billing_state": "OK"}, synchronize_session=False)
        )
        session.commit()
        invoice_paid_total.inc()
        logger.info("Invoice %s marked PAID", invoice_id)
        return Response(status_code=200)

    # failure ---------------------------------------------------------------
    if event["type"] in ("invoice.payment_failed", "invoice.payment_action_required"):
        final = data["status"] in ("past_due", "uncollectible")
        if final:
            (
                session.query(Recharge)
                .filter_by(stripe_invoice_id=invoice_id)
                .update({"status": RechargeStatus.FAILED}, synchronize_session=False)
            )
            (
                session.query(User)
                .filter(User.id.in_(user_ids_subq))
                .update({"billing_state": "PAST_DUE"}, synchronize_session=False)
            )
        session.commit()
        invoice_failed_total.inc()
        logger.info("Invoice %s marked FAILED", invoice_id)
        return Response(status_code=200)

    # any other invoice.* variant ------------------------------------------
    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def handle_event(event: Dict) -> Response:  # convenience wrapper
    """Open a short-lived session and delegate to `process_invoice_event`."""
    with SessionLocal() as session:
        return process_invoice_event(event, session)
