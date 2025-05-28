import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.webhooks.stripe import process_invoice_event

router = APIRouter()


@router.post("/webhooks/stripe", include_in_schema=False)
async def handle_stripe_webhook(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """
    Handle Stripe webhook events to update user credits based on payment outcomes.
    """
    webhook_log_dao = WebhookLogDAO(session)

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_LIVE")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

    # For local development, allow skipping signature verification
    SKIP_SIGNATURE_VERIFICATION = (
        os.environ.get("SKIP_STRIPE_SIGNATURE_VERIFICATION", "false").lower() == "true"
    )

    if SKIP_SIGNATURE_VERIFICATION:
        # In local development mode, parse the payload directly
        import json

        try:
            event = json.loads(payload.decode("utf-8"))
            logging.info("Skipping Stripe signature verification for local development")
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON payload: {e}")
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
            logging.error(f"Invalid payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logging.error(f"Signature verification failed: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type")
    event_id = event.get("id")

    # Check for idempotency - if we've already processed this event, skip it
    existing = webhook_log_dao.event_exists(event_id)
    if existing:
        logging.info(f"Event {event_id} already processed. Skipping.")
        return {"status": "ok"}

    # Process invoice events using the dedicated handler
    if event_type.startswith("invoice."):
        response = process_invoice_event(event, session)
        return {"status": "ok"}
    else:
        logging.info(f"Unhandled event type: {event_type}")
        # Log the event for idempotency even if we don't process it
        webhook_log_dao.create_webhook_log(event_id, event_type)
        return {"status": "ok"}
