"""Stripe webhook handler (import-safe).

The module exposes:
• process_webhook_event(event, session)  – core logic (pure, test-friendly)
• handle_event(event)                    – convenience wrapper that opens
                                           its own DB session
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Dict

import stripe
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.models.orchestra_models import WebhookLog
from orchestra.lib.billing import get_appropriate_stripe_key
from orchestra.observability.metrics import invoice_failed_total, invoice_paid_total
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


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

    # Get user_id for metrics (take first one since all recharges for same invoice have same user)
    user_id = session.execute(
        select(Recharge.user_id)
        .where(Recharge.stripe_invoice_id == invoice_id)
        .limit(1),
    ).scalar()

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
        if user_id:
            invoice_paid_total.labels(user_id=user_id).inc()
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
        if user_id:
            invoice_failed_total.labels(user_id=user_id).inc()
        logger.info("Invoice %s marked FAILED", invoice_id)
        return Response(status_code=200)

    # any other invoice.* variant ------------------------------------------
    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def process_charge_event(event: Dict, session: Session) -> Response:  # noqa: D401
    """Business logic for *charge.* events coming from Stripe webhooks."""
    users_dao = UsersDAO(session)
    recharge_dao = RechargeDAO(session)
    webhook_log_dao = WebhookLogDAO(session)

    event_type = event.get("type")
    event_id = event.get("id")
    data_object = event.get("data", {}).get("object", {})

    # idempotency guard -----------------------------------------------------
    if webhook_log_dao.event_exists(event_id):
        return Response(status_code=200)

    if event_type in ("charge.refunded", "charge.refund.updated"):
        # Dispute -> PaymentIntent (metadata) -> User
        payment_intent_id = data_object.get("payment_intent")
        payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        user_id = payment_intent.get("metadata", {}).get("user_id")
        try:
            credits_original = float(
                payment_intent.get("metadata", {}).get("credits_purchased", 0),
            )
        except Exception as e:
            logger.error(f"Invalid credits_purchased data: {e}")
            credits_original = 0
        total_charge_cents = data_object.get("amount")
        total_refunded_cents = data_object.get("amount_refunded", 0)

        if user_id and credits_original and total_charge_cents:
            fraction = total_refunded_cents / float(total_charge_cents)
            credits_to_remove = credits_original * fraction
            try:
                # Update the Recharge record status to 'refunded' or 'partially_refunded'
                invoice_id = data_object.get("invoice")
                if invoice_id:
                    status = "refunded" if fraction == 1.0 else "partially_refunded"
                    recharge = recharge_dao.get_recharge_by_transaction_id(invoice_id)
                    if recharge:
                        recharge_dao.update_recharge_status(recharge.id, status)

                users_dao.recharge_credit(user_id, -credits_to_remove)
                logger.info(
                    f"User {user_id} debited with {credits_to_remove} credits due to refund (fraction: {fraction}).",
                )
            except Exception as e:
                logger.error(f"Failed to debit user {user_id} on refund: {e}")

    elif event_type in ("charge.dispute.created", "charge.dispute.funds_withdrawn"):
        # Charge -> PaymentIntent (metadata) -> User
        payment_intent_id = data_object.get("payment_intent")
        payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        user_id = payment_intent.get("metadata", {}).get("user_id")
        try:
            credits_original = float(
                payment_intent.get("metadata", {}).get("credits_purchased", 0),
            )
        except Exception as e:
            logger.error(f"Invalid credits_purchased on dispute event: {e}")
            credits_original = 0
        if user_id and credits_original > 0:
            try:
                # Update the Recharge record status to 'disputed'
                invoice_id = payment_intent.get("invoice")
                if invoice_id:
                    recharge = recharge_dao.get_recharge_by_transaction_id(invoice_id)
                    if recharge:
                        recharge_dao.update_recharge_status(recharge.id, "disputed")

                users_dao.recharge_credit(user_id, -credits_original)
                logger.info(
                    f"User {user_id} debited with {credits_original} credits due to dispute event.",
                )
            except Exception as e:
                logger.error(f"Failed to debit user {user_id} on dispute: {e}")

    # Log the event for idempotency
    webhook_log_dao.create_webhook_log(event_id, event_type)
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def handle_event_core(event: Dict, session: Session) -> Response:  # noqa: D401
    """Main dispatcher for all Stripe webhook events."""
    event_type = event.get("type", "")
    if event_type.startswith("invoice."):
        return process_invoice_event(event, session)
    elif event_type.startswith("charge."):
        return process_charge_event(event, session)
    else:
        # Log unhandled events for idempotency
        webhook_log_dao = WebhookLogDAO(session)
        event_id = event.get("id")
        if not webhook_log_dao.event_exists(event_id):
            webhook_log_dao.create_webhook_log(event_id, event_type)
        logger.debug(f"Unhandled event type: {event_type}")
        return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def handle_event(event: Dict) -> Response:  # convenience wrapper
    """Open a short-lived session and delegate to `handle_event_core`."""
    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        return handle_event_core(event, session)


@router.post("/webhooks/stripe", include_in_schema=False)
async def handle_stripe_webhook(request: Request):
    """Handle Stripe webhook events to update user credits based on payment outcomes."""
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    # Use intelligent key selection - prefer test keys in test environments
    stripe_key = get_appropriate_stripe_key()
    if not stripe_key:
        logger.error("No valid Stripe API key found")
        raise HTTPException(status_code=500, detail="Stripe configuration error")

    stripe.api_key = stripe_key
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

    # For local development, allow skipping signature verification
    SKIP_SIGNATURE_VERIFICATION = (
        os.environ.get("SKIP_STRIPE_SIGNATURE_VERIFICATION", "false").lower() == "true"
    )

    if SKIP_SIGNATURE_VERIFICATION:
        # In local development mode, parse the payload directly
        try:
            event = json.loads(payload.decode("utf-8"))
            logger.info("Skipping Stripe signature verification for local development")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
    else:
        # Production mode - verify signature
        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET,
                tolerance=600,  # Increase tolerance to 10 minutes for local development
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Signature verification failed: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature")

    # Process all events using the unified handler
    response = handle_event(event)
    return {"status": "ok"}
