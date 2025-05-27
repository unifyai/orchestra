import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.models.orchestra_models import Recharge, RechargeStatus

router = APIRouter()


@router.post("/webhooks/stripe", include_in_schema=False)
async def handle_stripe_webhook(
    request: Request,
    users_dao: UsersDAO = Depends(UsersDAO),
    recharge_dao: RechargeDAO = Depends(RechargeDAO),
    webhook_log_dao: WebhookLogDAO = Depends(WebhookLogDAO),
):
    """
    Handle Stripe webhook events to update user credits based on payment outcomes.
    """
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_LIVE")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError as e:
        logging.error(f"Invalid payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        logging.error(f"Signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type")
    event_id = event.get("id")
    data_object = event.get("data", {}).get("object", {})

    # Check for idempotency - if we've already processed this event, skip it
    existing = webhook_log_dao.event_exists(event_id)
    if existing:
        logging.info(f"Event {event_id} already processed. Skipping.")
        return {"status": "ok"}

    # Process events
    if event_type in ("invoice.paid", "invoice.payment_succeeded"):
        # Invoice (metadata) -> User
        user_id = data_object.get("metadata", {}).get("user_id")
        try:
            credits_purchased = float(
                data_object.get("metadata", {}).get("credits_purchased", 0),
            )
        except Exception as e:
            logging.error(f"Invalid credits_purchased value: {e}")
            credits_purchased = 0
        invoice_id = data_object.get("id")
        if not user_id:
            logging.warning("Received invoice.paid with missing user_id metadata.")
            return {"status": "ok"}

        # Retrieve recharge record by stripe_invoice_id (not transaction_id)
        recharge = recharge_dao.session.execute(
            select(Recharge).where(Recharge.stripe_invoice_id == invoice_id),
        ).scalar()

        if recharge and recharge.status in (
            RechargeStatus.INVOICE_CREATED,
            RechargeStatus.PENDING_INVOICE,
        ):
            # Mark the recharge as PAID (not "completed")
            recharge_dao.update_recharge_status(recharge.id, RechargeStatus.PAID.value)
            try:
                users_dao.recharge_credit(user_id, credits_purchased)
                logging.info(
                    f"User {user_id} credited with {credits_purchased} credits (invoice {invoice_id} completed).",
                )
            except Exception as e:
                logging.error(f"Failed to credit user {user_id}: {e}")
        else:
            logging.info(
                f"No pending recharge found for invoice {invoice_id} or already processed.",
            )
    elif event_type == "invoice.payment_failed":
        logging.warning(f"Invoice payment failed: {data_object.get('id')}")
        # Update pending recharge to 'failed' if it exists
        invoice_id = data_object.get("id")
        recharge = recharge_dao.session.execute(
            select(Recharge).where(Recharge.stripe_invoice_id == invoice_id),
        ).scalar()
        if recharge and recharge.status in (
            RechargeStatus.INVOICE_CREATED,
            RechargeStatus.PENDING_INVOICE,
        ):
            recharge_dao.update_recharge_status(
                recharge.id,
                RechargeStatus.FAILED.value,
            )
    elif event_type in ("charge.refunded", "charge.refund.updated"):
        # Dispute -> PaymentIntent (metadata) -> User
        payment_intent_id = data_object.get("payment_intent")
        payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        user_id = payment_intent.get("metadata", {}).get("user_id")
        try:
            credits_original = float(
                payment_intent.get("metadata", {}).get("credits_purchased", 0),
            )
        except Exception as e:
            logging.error(f"Invalid credits_purchased data: {e}")
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
                logging.info(
                    f"User {user_id} debited with {credits_to_remove} credits due to refund (fraction: {fraction}).",
                )
            except Exception as e:
                logging.error(f"Failed to debit user {user_id} on refund: {e}")
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
            logging.error(f"Invalid credits_purchased on dispute event: {e}")
            credits_original = 0
        if user_id and credits_original > 0:
            try:
                # Update the Recharge record status to 'disputed'
                invoice_id = payment_intent.get("invoice")
                if invoice_id:
                    recharge = recharge_dao.get_recharge_by_transaction_id(invoice_id)
                    recharge_dao.update_recharge_status(recharge.id, "disputed")

                users_dao.recharge_credit(user_id, -credits_original)
                logging.info(
                    f"User {user_id} debited with {credits_original} credits due to dispute event.",
                )
            except Exception as e:
                logging.error(f"Failed to debit user {user_id} on dispute: {e}")
    elif event_type == "invoice.voided":
        logging.info(f"Invoice voided: {data_object.get('id')}")
        # Update any pending recharge to 'voided'
        invoice_id = data_object.get("id")
        recharge = recharge_dao.session.execute(
            select(Recharge).where(Recharge.stripe_invoice_id == invoice_id),
        ).scalar()
        if recharge and recharge.status in (
            RechargeStatus.INVOICE_CREATED,
            RechargeStatus.PENDING_INVOICE,
        ):
            # Note: "voided" is not in the enum, so we'll use FAILED as the closest equivalent
            recharge_dao.update_recharge_status(
                recharge.id,
                RechargeStatus.FAILED.value,
            )
    else:
        logging.info(f"Unhandled event type: {event_type}")

    # Log the event in WebhookLog table for idempotency
    webhook_log_dao.create_webhook_log(event_id, event_type)

    return {"status": "ok"}
