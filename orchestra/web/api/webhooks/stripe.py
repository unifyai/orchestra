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

from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.organization_billing_dao import OrganizationBillingDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.models.orchestra_models import Organization, Recharge, RechargeStatus
from orchestra.db.models.orchestra_models import Users as User
from orchestra.db.models.orchestra_models import WebhookLog
from orchestra.web.api.utils.prometheus_middleware import (
    INVOICE_FAILED_TOTAL,
    INVOICE_PAID_TOTAL,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────
def process_checkout_session_event(
    event: Dict,
    session: Session,
) -> Response:  # noqa: D401
    """Business logic for *checkout.session.* events."""
    data = event["data"]["object"]
    event_id: str = event["id"]

    # Idempotency guard
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

    if event["type"] == "checkout.session.completed":
        # Subscriptions are handled by the monthly invoicer, so ignore them here
        if data.get("subscription"):
            session.commit()
            return Response(status_code=200)

        # Handle one-time payments
        # Check metadata for organization_id (direct org billing)
        metadata = data.get("metadata", {})
        organization_id = metadata.get("organization_id")
        user_id = data.get("client_reference_id")
        amount_total = data.get("amount_total")

        if amount_total is None:
            logger.error(
                {
                    "message": "checkout.session.completed event missing amount_total",
                    "event_id": event_id,
                },
            )
            session.commit()
            return Response(status_code=400)

        credits = amount_total / 100  # Assuming 1 credit = $1 and amount is in cents

        try:
            # Handle organization checkout (direct org billing)
            if organization_id:
                org_billing_dao = OrganizationBillingDAO(session)
                org = org_billing_dao.get(int(organization_id))

                if not org:
                    logger.error(
                        {
                            "message": "Organization not found for checkout",
                            "organization_id": organization_id,
                            "event_id": event_id,
                        },
                    )
                    session.commit()
                    return Response(status_code=404)

                # Enable direct billing if this is the org's first checkout
                if not org.stripe_customer_id:
                    stripe_customer_id = data.get("customer")
                    if stripe_customer_id:
                        org_billing_dao.set_stripe_customer_id(
                            int(organization_id),
                            stripe_customer_id,
                        )
                        logger.info(
                            {
                                "message": "Organization direct billing enabled",
                                "organization_id": organization_id,
                                "stripe_customer_id": stripe_customer_id,
                            },
                        )

                org_billing_dao.add_credits(int(organization_id), credits)
                logger.info(
                    {
                        "message": "Organization credited",
                        "organization_id": organization_id,
                        "credits": credits,
                    },
                )

            # Handle user checkout (personal or delegated billing)
            elif user_id:
                users_dao = UsersDAO(session)
                user = users_dao.get_user_with_id(user_id)
                users_dao.recharge_credit(user_id, credits)
                logger.info(
                    {"message": "User credited", "user_id": user_id, "credits": credits},
                )

            else:
                logger.error(
                    {
                        "message": "checkout.session.completed missing both user_id and organization_id",
                        "event_id": event_id,
                    },
                )
                session.commit()
                return Response(status_code=400)

        except HTTPException as e:
            if e.status_code == 404:
                logger.error(
                    {
                        "message": "Entity not found for checkout",
                        "user_id": user_id,
                        "organization_id": organization_id,
                        "event_id": event_id,
                    },
                )
                session.commit()
                return Response(status_code=404)
            logger.error(
                {
                    "message": "Unexpected HTTPException during credit recharge",
                    "error": f"{e.status_code}: {e.detail}",
                    "user_id": user_id,
                    "organization_id": organization_id,
                },
            )
            session.rollback()
            raise

        except Exception as e:
            logger.error(
                {
                    "message": "Failed to update credits",
                    "user_id": user_id,
                    "organization_id": organization_id,
                    "error": str(e),
                },
            )
            session.rollback()
            raise

    session.commit()
    return Response(status_code=200)


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

    # Get recharges for this invoice - could be user OR organization recharges
    recharges = (
        session.query(Recharge)
        .filter_by(stripe_invoice_id=invoice_id)
        .all()
    )

    # Determine if these are user or organization recharges
    user_ids = set()
    org_ids = set()
    for recharge in recharges:
        if recharge.user_id:
            user_ids.add(recharge.user_id)
        if recharge.organization_id:
            org_ids.add(recharge.organization_id)

    # For metrics, use first user_id found
    user_id = next(iter(user_ids), None)
    org_id = next(iter(org_ids), None)

    # Build subqueries for user updates
    user_ids_subq = (
        select(Recharge.user_id)
        .where(Recharge.stripe_invoice_id == invoice_id)
        .where(Recharge.user_id.isnot(None))
        .scalar_subquery()
    )

    org_ids_subq = (
        select(Recharge.organization_id)
        .where(Recharge.stripe_invoice_id == invoice_id)
        .where(Recharge.organization_id.isnot(None))
        .scalar_subquery()
    )

    # success ---------------------------------------------------------------
    if event["type"] == "invoice.payment_succeeded":
        # Update all recharges to PAID
        (
            session.query(Recharge)
            .filter_by(stripe_invoice_id=invoice_id)
            .update({"status": RechargeStatus.PAID}, synchronize_session=False)
        )

        # Update user billing state
        if user_ids:
            (
                session.query(User)
                .filter(User.id.in_(user_ids_subq))
                .update({"billing_state": "OK"}, synchronize_session=False)
            )

        # Update organization account status
        if org_ids:
            (
                session.query(Organization)
                .filter(Organization.id.in_(org_ids_subq))
                .update({"account_status": "ACTIVE"}, synchronize_session=False)
            )

        session.commit()
        if user_id:
            INVOICE_PAID_TOTAL.labels(user_id=user_id).inc()
        logger.info(
            {
                "message": "Invoice marked PAID",
                "invoice_id": invoice_id,
                "user_id": user_id,
                "organization_id": org_id,
            },
        )
        return Response(status_code=200)

    # failure ---------------------------------------------------------------
    if event["type"] in ("invoice.payment_failed", "invoice.payment_action_required"):
        final = data["status"] in ("past_due", "uncollectible")
        if final:
            # Update recharges to FAILED
            (
                session.query(Recharge)
                .filter_by(stripe_invoice_id=invoice_id)
                .update({"status": RechargeStatus.FAILED}, synchronize_session=False)
            )

            # Update user billing state
            if user_ids:
                (
                    session.query(User)
                    .filter(User.id.in_(user_ids_subq))
                    .update({"billing_state": "PAST_DUE"}, synchronize_session=False)
                )

            # Update organization account status
            if org_ids:
                (
                    session.query(Organization)
                    .filter(Organization.id.in_(org_ids_subq))
                    .update({"account_status": "PAST_DUE"}, synchronize_session=False)
                )

        session.commit()
        if user_id:
            INVOICE_FAILED_TOTAL.labels(user_id=user_id).inc()
        logger.info(
            {
                "message": "Invoice marked FAILED",
                "invoice_id": invoice_id,
                "user_id": user_id,
                "organization_id": org_id,
            },
        )
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
        try:
            # NOTE: Linter may flag the following line, but it is valid
            # with the official Stripe Python library.
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            user_id = payment_intent.get("metadata", {}).get("user_id")
            try:
                credits_original = float(
                    payment_intent.get("metadata", {}).get("credits_purchased", 0),
                )
            except Exception as e:
                logger.error(
                    {
                        "message": "Invalid credits_purchased data",
                        "payment_intent_id": payment_intent_id,
                        "error": str(e),
                    },
                )
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
                        recharge = recharge_dao.get_recharge_by_transaction_id(
                            invoice_id,
                        )
                        if recharge:
                            recharge_dao.update_recharge_status(recharge.id, status)

                    users_dao.recharge_credit(user_id, -credits_to_remove)
                    logger.info(
                        {
                            "message": "User debited due to refund",
                            "user_id": user_id,
                            "credits_removed": credits_to_remove,
                            "refund_fraction": fraction,
                        },
                    )
                except Exception as e:
                    logger.error(
                        {
                            "message": "Failed to debit user on refund",
                            "user_id": user_id,
                            "error": str(e),
                        },
                    )
        except stripe.error.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent for refund",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )

    elif event_type in ("charge.dispute.created", "charge.dispute.funds_withdrawn"):
        payment_intent_id = data_object.get("payment_intent")
        try:
            # NOTE: Linter may flag the following line, but it is valid
            # with the official Stripe Python library.
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            invoice_id = payment_intent.get("invoice")

            # Direct credit purchase (user_id in PaymentIntent metadata)
            user_id = payment_intent.get("metadata", {}).get("user_id")
            try:
                credits_original = float(
                    payment_intent.get("metadata", {}).get("credits_purchased", 0),
                )
            except Exception as e:
                logger.error(
                    {
                        "message": "Invalid credits_purchased on dispute event",
                        "payment_intent_id": payment_intent_id,
                        "error": str(e),
                    },
                )
                credits_original = 0

            if user_id and credits_original > 0:
                try:
                    if invoice_id:
                        recharge = recharge_dao.get_recharge_by_transaction_id(
                            invoice_id,
                        )
                        if recharge:
                            recharge_dao.update_recharge_status(recharge.id, "disputed")

                    users_dao.recharge_credit(user_id, -credits_original)

                    # Auto-suspend user for disputing one-time purchase
                    session.query(User).filter(User.id == user_id).update(
                        {"billing_state": "SUSPENDED"},
                        synchronize_session=False,
                    )
                    session.commit()

                    logger.info(
                        {
                            "message": "User debited and suspended due to dispute on direct purchase",
                            "user_id": user_id,
                            "credits_removed": credits_original,
                        },
                    )
                except Exception as e:
                    logger.error(
                        {
                            "message": "Failed to debit and suspend user on dispute",
                            "user_id": user_id,
                            "error": str(e),
                        },
                    )

            elif invoice_id:
                # Monthly invoice dispute (lookup recharges by invoice ID)
                try:
                    recharges = (
                        session.query(Recharge)
                        .filter_by(stripe_invoice_id=invoice_id)
                        .all()
                    )

                    if recharges:
                        total_credits = sum(float(r.quantity) for r in recharges)
                        user_id = recharges[0].user_id

                        # Update recharge statuses to DISPUTED
                        session.query(Recharge).filter_by(
                            stripe_invoice_id=invoice_id,
                        ).update(
                            {"status": RechargeStatus.DISPUTED},
                            synchronize_session=False,
                        )

                        # Debit user's credits
                        users_dao.recharge_credit(user_id, -total_credits)

                        # Auto-suspend user for disputing monthly invoice
                        session.query(User).filter(User.id == user_id).update(
                            {"billing_state": "SUSPENDED"},
                            synchronize_session=False,
                        )
                        session.commit()

                        logger.info(
                            {
                                "message": "User debited and suspended due to dispute on monthly invoice",
                                "user_id": user_id,
                                "credits_removed": total_credits,
                                "invoice_id": invoice_id,
                                "recharges_updated": len(recharges),
                            },
                        )
                    else:
                        logger.warning(
                            {
                                "message": "No recharges found for disputed invoice",
                                "invoice_id": invoice_id,
                            },
                        )
                except Exception as e:
                    logger.error(
                        {
                            "message": "Failed to handle monthly invoice dispute",
                            "invoice_id": invoice_id,
                            "error": str(e),
                        },
                    )
            else:
                logger.warning(
                    {
                        "message": "Dispute event missing user_id and invoice_id",
                        "payment_intent_id": payment_intent_id,
                    },
                )
        except stripe.error.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent for dispute",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )

    # Log the event for idempotency
    webhook_log_dao.create_webhook_log(event_id, event_type)
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def process_review_event(event: Dict, session: Session) -> Response:
    """Business logic for *review.* events from Stripe."""
    data = event["data"]["object"]
    event_id: str = event["id"]
    payment_intent_id = data.get("payment_intent")

    # Idempotency guard
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

    user_id = None
    if payment_intent_id:
        try:
            # NOTE: Linter may flag the following line, but it is valid
            # with the official Stripe Python library.
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            user_id = payment_intent.get("metadata", {}).get("user_id")
        except stripe.error.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )
            # Still commit webhook log and return 200 to avoid retries for this
            session.commit()
            return Response(status_code=200)

    log_payload = {
        "event_id": event_id,
        "event_type": event["type"],
        "payment_intent_id": payment_intent_id,
        "user_id": user_id,
    }

    if event["type"] == "review.opened":
        logger.info({**log_payload, "message": "Charge review opened."})

    elif event["type"] == "review.closed":
        close_reason = data.get("closed_reason")
        logger.info(
            {
                **log_payload,
                "message": "Charge review closed.",
                "closed_reason": close_reason,
            },
        )

    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def process_customer_tax_id_event(event: Dict, session: Session) -> Response:
    """Business logic for customer.tax_id.* events from Stripe."""
    data = event["data"]["object"]
    event_id: str = event["id"]

    # Idempotency guard
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

    # Extract tax ID information
    customer_id = data.get("customer")
    tax_id_value = data.get("value")
    tax_id_type = data.get("type")

    if not customer_id:
        print(
            f"WARNING: Tax ID event missing customer_id. "
            f"Event ID: {event_id}, Customer ID: {customer_id}",
        )
        session.commit()
        return Response(status_code=200)

    # For deletion events, tax_id_value might be None, which is OK
    if event["type"] != "customer.tax_id.deleted" and not tax_id_value:
        print(
            f"WARNING: Tax ID event missing value (required for create/update). "
            f"Event ID: {event_id}, Customer ID: {customer_id}, Event Type: {event['type']}",
        )
        session.commit()
        return Response(status_code=200)

    try:
        # Find user by Stripe customer ID
        users_dao = UsersDAO(session)
        auth_user_dao = AuthUserDAO(session)

        billing_user = users_dao.get_user_by_stripe_id(customer_id)
        if not billing_user:
            print(
                f"WARNING: No user found for Stripe customer ID. "
                f"Customer ID: {customer_id}, Event ID: {event_id}",
            )
            session.commit()
            return Response(status_code=200)

        # Get auth user to check if it's a business account
        auth_user_row = auth_user_dao.get_by_id(billing_user.id)
        if not auth_user_row:
            print(
                f"WARNING: No auth user found for billing user. "
                f"User ID: {billing_user.id}, Event ID: {event_id}",
            )
            session.commit()
            return Response(status_code=200)

        auth_user = auth_user_row[0]

        # Only process for business accounts
        if auth_user.account_type != "business":
            print(
                f"INFO: Tax ID event for non-business account, skipping. "
                f"User ID: {billing_user.id}, Account Type: {auth_user.account_type}, Event ID: {event_id}",
            )
            session.commit()
            return Response(status_code=200)

        # Handle different event types
        if event["type"] == "customer.tax_id.created":
            # Update the user's tax ID from Stripe
            auth_user_dao.update(
                id=billing_user.id,
                tax_id=tax_id_value,
            )

            # Determine and set tax jurisdiction based on tax ID type
            tax_jurisdiction = None
            if tax_id_type == "eu_vat":
                tax_jurisdiction = "EU"
            elif tax_id_type == "gb_vat":
                tax_jurisdiction = "UK"
            elif tax_id_type == "au_abn":
                tax_jurisdiction = "AU"
            elif tax_id_type == "us_ein":
                tax_jurisdiction = "US"
            elif tax_id_type == "ca_gst_hst":
                tax_jurisdiction = "CA"

            if tax_jurisdiction:
                auth_user_dao.update(
                    id=billing_user.id,
                    tax_jurisdiction=tax_jurisdiction,
                )

            logger.info(
                {
                    "message": "Tax ID synced from Stripe to database",
                    "user_id": billing_user.id,
                    "tax_id_type": tax_id_type,
                    "tax_jurisdiction": tax_jurisdiction,
                    "event_id": event_id,
                },
            )

        elif event["type"] == "customer.tax_id.updated":
            # Update the user's tax ID from Stripe
            auth_user_dao.update(
                id=billing_user.id,
                tax_id=tax_id_value,
            )

            logger.info(
                {
                    "message": "Tax ID updated from Stripe",
                    "user_id": billing_user.id,
                    "tax_id_type": tax_id_type,
                    "event_id": event_id,
                },
            )

        elif event["type"] == "customer.tax_id.deleted":
            # Clear the user's tax ID
            auth_user_dao.update(
                id=billing_user.id,
                tax_id=None,
                tax_jurisdiction=None,
            )

            logger.info(
                {
                    "message": "Tax ID cleared from database after Stripe deletion",
                    "user_id": billing_user.id,
                    "event_id": event_id,
                },
            )

        session.commit()
        return Response(status_code=200)

    except Exception as e:
        logger.error(
            {
                "message": "Error processing tax ID event",
                "event_id": event_id,
                "customer_id": customer_id,
                "error": str(e),
            },
        )
        session.rollback()
        raise


# ──────────────────────────────────────────────────────────────────────────
def handle_event_core(event: Dict, session: Session) -> Response:  # noqa: D401
    """Main dispatcher for all Stripe webhook events."""
    event_type = event.get("type", "")
    if event_type.startswith("checkout.session."):
        return process_checkout_session_event(event, session)
    if event_type.startswith("invoice."):
        return process_invoice_event(event, session)
    elif event_type.startswith("review."):
        return process_review_event(event, session)
    elif event_type.startswith("charge."):
        return process_charge_event(event, session)
    elif event_type.startswith("customer.tax_id."):
        return process_customer_tax_id_event(event, session)
    else:
        # Log unhandled events for idempotency
        webhook_log_dao = WebhookLogDAO(session)
        event_id = event.get("id")
        if not webhook_log_dao.event_exists(event_id):
            webhook_log_dao.create_webhook_log(event_id, event_type)
        logger.debug(
            {
                "message": "Unhandled event type",
                "event_type": event_type,
                "event_id": event_id,
            },
        )
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

    # Configure Stripe API key
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_key:
        logger.error({"message": "STRIPE_SECRET_KEY environment variable not set"})
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
            logger.info(
                {
                    "message": "Skipping Stripe signature verification for local development",
                },
            )
        except json.JSONDecodeError as e:
            logger.error({"message": "Invalid JSON payload", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid payload")
    else:
        if not STRIPE_WEBHOOK_SECRET:
            logger.error(
                {
                    "message": "STRIPE_WEBHOOK_SECRET environment variable not set, but required for signature verification",
                },
            )
            raise HTTPException(
                status_code=500,
                detail="Stripe configuration error: Missing webhook secret",
            )

        # Production mode - verify signature
        try:
            # NOTE: Linter may flag the following line, but it is valid
            # with the official Stripe Python library.
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET,
                tolerance=600,  # Increase tolerance to 10 minutes for local development
            )
        except ValueError as e:
            logger.error({"message": "Invalid payload", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError as e:
            logger.error({"message": "Signature verification failed", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid signature")

    # Process all events using the unified handler
    return handle_event(event)
