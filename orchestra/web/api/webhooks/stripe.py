"""Stripe webhook handler (import-safe).

The module exposes:
• process_webhook_event(event, session)  – core logic (pure, test-friendly)
• handle_event(event)                    – convenience wrapper that opens
                                           its own DB session

All billing lookups now go through BillingAccount.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Dict

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_PAYMENT,
    BillingAccount,
    Recharge,
    RechargeStatus,
    User,
    WebhookLog,
)
from orchestra.settings import settings
from orchestra.web.api.utils.prometheus_middleware import (
    INVOICE_FAILED_TOTAL,
    INVOICE_PAID_TOTAL,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _sync_billing_account_metadata(
    stripe_customer_id: str,
    billing_account_id: int,
    *,
    user_id: str | None = None,
    organization_id: str | int | None = None,
) -> None:
    """Best-effort update of Stripe customer metadata with billing_account_id.

    Also ensures user_id / organization_id are present in metadata for
    cross-reference.  Failures are logged but never bubble up — we don't
    want a Stripe API hiccup to break credit granting.
    """
    try:
        metadata: dict[str, str] = {
            "billing_account_id": str(billing_account_id),
        }
        if user_id:
            metadata["user_id"] = user_id
        if organization_id:
            metadata["organization_id"] = str(organization_id)

        stripe.Customer.modify(stripe_customer_id, metadata=metadata)
        logger.info(
            {
                "message": "Stripe customer metadata updated with billing_account_id",
                "stripe_customer_id": stripe_customer_id,
                "billing_account_id": billing_account_id,
            },
        )
    except Exception as e:
        logger.warning(
            {
                "message": "Failed to update Stripe customer metadata (non-fatal)",
                "stripe_customer_id": stripe_customer_id,
                "error": str(e),
            },
        )


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

        credits = amount_total / 100  # 1 credit = $1, amount in cents

        # Update payment_intent metadata with the actual credits purchased.
        # The session was created with a default quantity, but the user may
        # have adjusted it via Stripe's quantity picker.
        payment_intent_id = data.get("payment_intent")
        if payment_intent_id:
            try:
                stripe.PaymentIntent.modify(
                    payment_intent_id,
                    metadata={"credits_purchased": str(credits)},
                )
            except Exception as e:
                logger.warning(
                    {
                        "message": "Failed to update credits_purchased on PaymentIntent (non-fatal)",
                        "payment_intent_id": payment_intent_id,
                        "credits": credits,
                        "error": str(e),
                    },
                )

        try:
            # Handle organization checkout (direct org billing)
            if organization_id:
                org_dao = OrganizationDAO(session)
                org = org_dao.get(int(organization_id))

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

                # Get billing account (created eagerly in organization_dao.create;
                # fallback handles legacy orgs that may not have one yet)
                ba_dao = BillingAccountDAO(session)
                ba = org.billing_account
                if ba is None:
                    ba = ba_dao.create()
                    org.billing_account_id = ba.id
                    session.flush()

                # Enable direct billing if this is the org's first checkout.
                stripe_customer_id = data.get("customer")
                if stripe_customer_id and not ba.stripe_customer_id:
                    ba.stripe_customer_id = stripe_customer_id
                    logger.info(
                        {
                            "message": "Organization direct billing enabled",
                            "organization_id": organization_id,
                            "stripe_customer_id": stripe_customer_id,
                        },
                    )
                    # Tag the Stripe customer with the billing_account_id
                    # (useful in both test and live modes for cross-referencing)
                    _sync_billing_account_metadata(
                        stripe_customer_id,
                        ba.id,
                        organization_id=organization_id,
                    )

                ba_dao.add_credits(ba.id, credits)

                # Record a PAID Recharge so checkout purchases count
                # toward the cumulative spending threshold for
                # auto-recharge eligibility.
                from decimal import Decimal as _Decimal

                checkout_recharge = Recharge(
                    billing_account_id=ba.id,
                    type=RECHARGE_TYPE_PAYMENT,
                    quantity=_Decimal(str(credits)),
                    amount_usd=_Decimal(str(credits)),
                    status=RechargeStatus.PAID,
                    stripe_invoice_id=data.get("invoice") or payment_intent_id,
                )
                session.add(checkout_recharge)

                session.flush()

                logger.info(
                    {
                        "message": "Organization credited",
                        "organization_id": organization_id,
                        "credits": credits,
                    },
                )

                AssistantContactDAO(session).maybe_clear_grace_period(ba)

                # Self-heal: restore PAST_DUE → ACTIVE if credits are now
                # positive.  Runs AFTER maybe_clear_grace_period (which
                # refreshes ba from DB and may itself restore status when
                # there are grace-period contacts).
                if ba.account_status == "PAST_DUE" and ba.credits > 0:
                    ba.account_status = "ACTIVE"
                    logger.info(
                        {
                            "message": "Organization restored to ACTIVE after checkout",
                            "organization_id": organization_id,
                            "credits": float(ba.credits),
                        },
                    )

            # Handle user checkout (personal billing)
            elif user_id:
                user_dao = UserDAO(session)
                user = user_dao.get_user_with_id(user_id)

                if not user:
                    logger.error(
                        {
                            "message": "User not found for checkout",
                            "user_id": user_id,
                            "event_id": event_id,
                        },
                    )
                    session.commit()
                    return Response(status_code=404)

                # Ensure user has a BillingAccount
                ba_dao = BillingAccountDAO(session)
                ba = user.billing_account
                if ba is None:
                    ba = ba_dao.create()
                    user.billing_account_id = ba.id
                    session.flush()

                # Save Stripe customer ID if this is the user's first checkout.
                stripe_customer_id = data.get("customer")
                if stripe_customer_id and not ba.stripe_customer_id:
                    ba.stripe_customer_id = stripe_customer_id
                    logger.info(
                        {
                            "message": "User Stripe customer ID saved",
                            "user_id": user_id,
                            "stripe_customer_id": stripe_customer_id,
                        },
                    )
                    # Tag the Stripe customer with the billing_account_id
                    # (useful in both test and live modes for cross-referencing)
                    _sync_billing_account_metadata(
                        stripe_customer_id,
                        ba.id,
                        user_id=user_id,
                    )

                ba_dao.add_credits(ba.id, credits)

                # Record a PAID Recharge so checkout purchases count
                # toward the cumulative spending threshold for
                # auto-recharge eligibility.
                from decimal import Decimal as _Decimal

                checkout_recharge = Recharge(
                    billing_account_id=ba.id,
                    type=RECHARGE_TYPE_PAYMENT,
                    quantity=_Decimal(str(credits)),
                    amount_usd=_Decimal(str(credits)),
                    status=RechargeStatus.PAID,
                    stripe_invoice_id=data.get("invoice") or payment_intent_id,
                )
                session.add(checkout_recharge)

                session.flush()

                logger.info(
                    {
                        "message": "User credited",
                        "user_id": user_id,
                        "credits": credits,
                    },
                )

                AssistantContactDAO(session).maybe_clear_grace_period(ba)

                if ba.account_status == "PAST_DUE" and ba.credits > 0:
                    ba.account_status = "ACTIVE"
                    logger.info(
                        {
                            "message": "User restored to ACTIVE after checkout",
                            "user_id": user_id,
                            "credits": float(ba.credits),
                        },
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


def _resolve_recharges_for_invoice(
    session: Session,
    invoice_id: str,
    metadata: Dict,
) -> list[Recharge]:
    """Find recharges for an invoice, self-healing if the invoicer's DB
    commit failed but the Stripe invoice was created.

    1. Look up recharges already linked to this ``invoice_id``.
    2. If none found, use the invoice metadata (``billing_account_id`` +
       ``invoice_group``) to find orphaned PENDING_INVOICE recharges and
       link them — making the system self-healing without reconciliation.
    """
    recharges = session.query(Recharge).filter_by(stripe_invoice_id=invoice_id).all()
    if recharges:
        return recharges

    ba_id_str = metadata.get("billing_account_id")
    invoice_group_str = metadata.get("invoice_group")
    if not ba_id_str or not invoice_group_str:
        return []

    try:
        import datetime as _dt

        ba_id = int(ba_id_str)
        invoice_group = _dt.date.fromisoformat(invoice_group_str)
    except (ValueError, TypeError):
        logger.warning(
            {
                "message": "Could not parse invoice metadata for self-heal",
                "invoice_id": invoice_id,
                "billing_account_id": ba_id_str,
                "invoice_group": invoice_group_str,
            },
        )
        return []

    orphans = (
        session.query(Recharge)
        .filter(
            Recharge.billing_account_id == ba_id,
            Recharge.status == RechargeStatus.PENDING_INVOICE,
            Recharge.invoice_group == invoice_group,
        )
        .all()
    )

    if orphans:
        for r in orphans:
            r.stripe_invoice_id = invoice_id
            r.status = RechargeStatus.INVOICE_CREATED
        session.flush()
        logger.info(
            {
                "message": "Self-healed orphaned recharges — linked to invoice",
                "invoice_id": invoice_id,
                "billing_account_id": ba_id,
                "recharges_linked": len(orphans),
            },
        )

    return orphans


def process_invoice_event(event: Dict, session: Session) -> Response:  # noqa: D401
    """Business logic for *invoice.* events coming from Stripe webhooks."""
    data = event["data"]["object"]
    invoice_id: str = data["id"]
    event_id: str = event["id"]

    # idempotency guard
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

    invoice_metadata = data.get("metadata", {})
    recharges = _resolve_recharges_for_invoice(
        session,
        invoice_id,
        invoice_metadata,
    )

    billing_account_ids = {r.billing_account_id for r in recharges}

    ba_ids_subq = (
        select(Recharge.billing_account_id)
        .where(Recharge.stripe_invoice_id == invoice_id)
        .scalar_subquery()
    )

    # ── success ──────────────────────────────────────────────────────────
    if event["type"] == "invoice.payment_succeeded":
        (
            session.query(Recharge)
            .filter_by(stripe_invoice_id=invoice_id)
            .update({"status": RechargeStatus.PAID}, synchronize_session=False)
        )

        if billing_account_ids:
            (
                session.query(BillingAccount)
                .filter(BillingAccount.id.in_(ba_ids_subq))
                .update({"account_status": "ACTIVE"}, synchronize_session=False)
            )

        for ba_id in billing_account_ids:
            ba = session.query(BillingAccount).filter_by(id=ba_id).first()
            if ba:
                AssistantContactDAO(session).maybe_clear_grace_period(ba)

        session.commit()
        for ba_id in billing_account_ids:
            INVOICE_PAID_TOTAL.labels(billing_account_id=str(ba_id)).inc()
        logger.info(
            {
                "message": "Invoice marked PAID",
                "invoice_id": invoice_id,
                "billing_account_ids": list(billing_account_ids),
            },
        )
        return Response(status_code=200)

    # ── failure ──────────────────────────────────────────────────────────
    if event["type"] in ("invoice.payment_failed", "invoice.payment_action_required"):
        # Disable auto-recharge on the *first* failure, not just the
        # final one.  This prevents new postpaid credits from being
        # granted while Stripe is retrying the existing invoice.
        if billing_account_ids:
            (
                session.query(BillingAccount)
                .filter(BillingAccount.id.in_(ba_ids_subq))
                .update({"autorecharge": False}, synchronize_session=False)
            )
            logger.info(
                {
                    "message": "Auto-recharge disabled due to payment failure",
                    "invoice_id": invoice_id,
                    "billing_account_ids": list(billing_account_ids),
                },
            )

        final = data["status"] in ("past_due", "uncollectible")
        if final:
            (
                session.query(Recharge)
                .filter_by(stripe_invoice_id=invoice_id)
                .update({"status": RechargeStatus.FAILED}, synchronize_session=False)
            )

            # Void the credits that were granted on auto-recharge but
            # never paid for.  This is safe because:
            #  • The recharges record exactly how many credits were loaned.
            #  • Deducting them may push the balance negative, which is
            #    the desired signal for PAST_DUE / eventual SUSPENDED.
            from decimal import Decimal as _Decimal

            for ba_id in billing_account_ids:
                unpaid = sum(
                    r.quantity for r in recharges if r.billing_account_id == ba_id
                )
                if unpaid:
                    ba = session.query(BillingAccount).filter_by(id=ba_id).first()
                    if ba:
                        ba.credits = ba.credits - _Decimal(str(unpaid))
                        logger.info(
                            {
                                "message": "Voided unpaid auto-recharge credits",
                                "billing_account_id": ba_id,
                                "credits_voided": float(unpaid),
                                "new_balance": float(ba.credits),
                            },
                        )

            # Void the Stripe invoice so the debt is considered settled
            # via credit deduction.  Without this, Stripe could later
            # collect the invoice (user updates card, pays hosted page)
            # and the user would be double-charged.
            try:
                stripe.Invoice.void_invoice(invoice_id)
                logger.info(
                    {
                        "message": "Voided Stripe invoice after credit deduction",
                        "invoice_id": invoice_id,
                    },
                )
            except stripe.StripeError as void_err:
                logger.warning(
                    {
                        "message": "Could not void Stripe invoice (non-fatal)",
                        "invoice_id": invoice_id,
                        "error": str(void_err),
                    },
                )

            if billing_account_ids:
                (
                    session.query(BillingAccount)
                    .filter(BillingAccount.id.in_(ba_ids_subq))
                    .update(
                        {"account_status": "PAST_DUE"},
                        synchronize_session=False,
                    )
                )

        session.commit()
        for ba_id in billing_account_ids:
            INVOICE_FAILED_TOTAL.labels(billing_account_id=str(ba_id)).inc()
        logger.info(
            {
                "message": "Invoice payment failed",
                "invoice_id": invoice_id,
                "final": final,
                "billing_account_ids": list(billing_account_ids),
            },
        )
        return Response(status_code=200)

    # any other invoice.* variant
    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def _resolve_billing_account_from_metadata(
    session: Session,
    metadata: Dict,
) -> BillingAccount | None:
    """
    Resolve the correct BillingAccount from PaymentIntent metadata.

    Checks for organization_id first (org checkout), then user_id (personal checkout).
    Returns None if no billing account can be found.
    """
    from orchestra.db.models.orchestra_models import Organization

    organization_id = metadata.get("organization_id")
    user_id = metadata.get("user_id")

    if organization_id:
        org = session.query(Organization).filter_by(id=int(organization_id)).first()
        if org and org.billing_account_id:
            return (
                session.query(BillingAccount)
                .filter_by(id=org.billing_account_id)
                .first()
            )

    if user_id:
        user = session.query(User).filter_by(id=user_id).first()
        if user and user.billing_account_id:
            return (
                session.query(BillingAccount)
                .filter_by(id=user.billing_account_id)
                .first()
            )

    return None


def process_charge_event(event: Dict, session: Session) -> Response:  # noqa: D401
    """Business logic for *charge.* events coming from Stripe webhooks.

    Covers refunds, disputes, and dispute closures.  The idempotency
    guard follows the same insert-early pattern used by checkout and
    invoice handlers: the ``WebhookLog`` row is added to the session
    and flushed before processing starts.  On success the final
    ``session.commit()`` persists both the log and any data changes.
    On failure a ``session.rollback()`` removes the log so Stripe can
    retry the event delivery.
    """
    billing_account_dao = BillingAccountDAO(session)
    recharge_dao = RechargeDAO(session)

    event_type = event.get("type")
    event_id = event.get("id")
    data_object = event.get("data", {}).get("object", {})

    # ── Idempotency guard ────────────────────────────────────────────
    if session.query(WebhookLog).filter_by(event_id=event_id).first():
        return Response(status_code=200)

    session.add(
        WebhookLog(
            id=str(uuid.uuid4()),
            event_id=event_id,
            event_type=event_type,
        ),
    )
    session.flush()

    # ── Refund ────────────────────────────────────────────────────────
    if event_type in ("charge.refunded", "charge.refund.updated"):
        payment_intent_id = data_object.get("payment_intent")
        if not payment_intent_id:
            logger.warning(
                {
                    "message": "Refund event has no payment_intent — skipping",
                    "event_id": event_id,
                },
            )
            session.commit()
            return Response(status_code=200)

        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        except stripe.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent for refund",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )
            session.rollback()
            raise

        pi_metadata = payment_intent.get("metadata", {})
        try:
            credits_original = float(
                pi_metadata.get("credits_purchased", 0),
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

        if credits_original and total_charge_cents:
            fraction = total_refunded_cents / float(total_charge_cents)
            credits_to_remove = credits_original * fraction

            invoice_id = data_object.get("invoice")
            if invoice_id:
                recharge = (
                    session.query(Recharge)
                    .filter_by(stripe_invoice_id=invoice_id)
                    .first()
                )
                if recharge and fraction >= 1.0:
                    recharge_dao.update_recharge_status(
                        recharge.id,
                        RechargeStatus.FAILED,
                    )

            ba = _resolve_billing_account_from_metadata(session, pi_metadata)
            if ba:
                billing_account_dao.deduct_credits(ba.id, credits_to_remove)
                logger.info(
                    {
                        "message": "Billing account debited due to refund",
                        "billing_account_id": ba.id,
                        "credits_removed": credits_to_remove,
                        "refund_fraction": fraction,
                    },
                )
            else:
                logger.error(
                    {
                        "message": "Could not resolve billing account for refund",
                        "payment_intent_id": payment_intent_id,
                        "metadata": pi_metadata,
                    },
                )

    # ── Dispute created / funds withdrawn ─────────────────────────────
    elif event_type in ("charge.dispute.created", "charge.dispute.funds_withdrawn"):
        payment_intent_id = data_object.get("payment_intent")
        if not payment_intent_id:
            logger.error(
                {
                    "message": "Dispute event has no payment_intent — cannot process",
                    "event_id": event_id,
                },
            )
            session.commit()
            return Response(status_code=200)

        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        except stripe.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent for dispute",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )
            session.rollback()
            raise

        invoice_id = payment_intent.get("invoice")
        pi_metadata = payment_intent.get("metadata", {})

        try:
            credits_original = float(
                pi_metadata.get("credits_purchased", 0),
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

        def _suspend_ba_for_dispute(ba: BillingAccount) -> None:
            ba.account_status = "SUSPENDED"
            ba.autorecharge = False

        if credits_original > 0:
            if invoice_id:
                recharge = (
                    session.query(Recharge)
                    .filter_by(stripe_invoice_id=invoice_id)
                    .first()
                )
                if recharge:
                    recharge_dao.update_recharge_status(
                        recharge.id,
                        RechargeStatus.DISPUTED,
                    )

            ba = _resolve_billing_account_from_metadata(session, pi_metadata)
            if ba:
                billing_account_dao.deduct_credits(ba.id, credits_original)
                _suspend_ba_for_dispute(ba)

                logger.info(
                    {
                        "message": "Billing account debited and suspended due to dispute",
                        "billing_account_id": ba.id,
                        "credits_removed": credits_original,
                    },
                )
            else:
                logger.error(
                    {
                        "message": "Could not resolve billing account for dispute",
                        "payment_intent_id": payment_intent_id,
                        "metadata": pi_metadata,
                    },
                )

        elif invoice_id:
            recharges = (
                session.query(Recharge).filter_by(stripe_invoice_id=invoice_id).all()
            )

            if recharges:
                total_credits = sum(float(r.quantity) for r in recharges)
                ba_id = recharges[0].billing_account_id

                session.query(Recharge).filter_by(
                    stripe_invoice_id=invoice_id,
                ).update(
                    {"status": RechargeStatus.DISPUTED},
                    synchronize_session=False,
                )

                billing_account_dao.deduct_credits(ba_id, total_credits)

                ba = session.query(BillingAccount).filter_by(id=ba_id).first()
                if ba:
                    _suspend_ba_for_dispute(ba)

                logger.info(
                    {
                        "message": "Billing account debited and suspended due to dispute",
                        "billing_account_id": ba_id,
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
        else:
            logger.warning(
                {
                    "message": "Dispute event missing metadata and invoice_id",
                    "payment_intent_id": payment_intent_id,
                },
            )

    # ── Dispute closed ────────────────────────────────────────────────
    elif event_type == "charge.dispute.closed":
        dispute_status = data_object.get("status")
        payment_intent_id = data_object.get("payment_intent")
        dispute_amount_cents = data_object.get("amount", 0)

        if dispute_status == "won" and payment_intent_id:
            try:
                payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            except stripe.StripeError as e:
                logger.error(
                    {
                        "message": "Failed to process won dispute",
                        "payment_intent_id": payment_intent_id,
                        "error": str(e),
                    },
                )
                session.rollback()
                raise

            pi_metadata = payment_intent.get("metadata", {})
            invoice_id = payment_intent.get("invoice")

            try:
                credits_original = float(
                    pi_metadata.get("credits_purchased", 0),
                )
            except Exception:
                credits_original = 0

            ba = _resolve_billing_account_from_metadata(session, pi_metadata)
            if not ba and invoice_id:
                recharges = (
                    session.query(Recharge)
                    .filter_by(stripe_invoice_id=invoice_id)
                    .all()
                )
                if recharges:
                    credits_original = sum(float(r.quantity) for r in recharges)
                    ba = (
                        session.query(BillingAccount)
                        .filter_by(id=recharges[0].billing_account_id)
                        .first()
                    )

            if ba and credits_original > 0:
                from decimal import Decimal

                ba.credits = ba.credits + Decimal(str(credits_original))

                if invoice_id:
                    session.query(Recharge).filter_by(
                        stripe_invoice_id=invoice_id,
                        status=RechargeStatus.DISPUTED,
                    ).update(
                        {"status": RechargeStatus.PAID},
                        synchronize_session=False,
                    )

                has_other_failed = (
                    session.query(Recharge)
                    .filter(
                        Recharge.billing_account_id == ba.id,
                        Recharge.status == RechargeStatus.FAILED,
                    )
                    .first()
                    is not None
                )
                if ba.account_status == "SUSPENDED" and not has_other_failed:
                    ba.account_status = "ACTIVE"

                logger.info(
                    {
                        "message": "Dispute won — credits re-credited",
                        "billing_account_id": ba.id,
                        "credits_restored": credits_original,
                        "account_status": ba.account_status,
                        "dispute_status": dispute_status,
                    },
                )
            elif ba:
                if ba.account_status == "SUSPENDED":
                    has_other_failed = (
                        session.query(Recharge)
                        .filter(
                            Recharge.billing_account_id == ba.id,
                            Recharge.status == RechargeStatus.FAILED,
                        )
                        .first()
                        is not None
                    )
                    if not has_other_failed:
                        ba.account_status = "ACTIVE"
                logger.info(
                    {
                        "message": "Dispute won — account evaluated (no credits to re-credit)",
                        "billing_account_id": ba.id,
                        "account_status": ba.account_status,
                    },
                )
            else:
                logger.warning(
                    {
                        "message": "Dispute won but could not resolve billing account",
                        "payment_intent_id": payment_intent_id,
                    },
                )
        elif dispute_status == "lost":
            logger.info(
                {
                    "message": "Dispute lost — no further action (already handled)",
                    "payment_intent_id": payment_intent_id,
                    "dispute_status": dispute_status,
                    "amount_cents": dispute_amount_cents,
                },
            )
        else:
            logger.info(
                {
                    "message": "Dispute closed with non-actionable status",
                    "dispute_status": dispute_status,
                    "payment_intent_id": payment_intent_id,
                },
            )

    session.commit()
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
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            user_id = payment_intent.get("metadata", {}).get("user_id")
        except stripe.StripeError as e:
            logger.error(
                {
                    "message": "Failed to retrieve PaymentIntent",
                    "payment_intent_id": payment_intent_id,
                    "error": str(e),
                },
            )
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
    """
    Handler for customer.tax_id.* events from Stripe.

    Syncs tax ID changes from Stripe to the corresponding BillingAccount.
    """
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
    tax_id_type = data.get("type")  # e.g. "eu_vat", "us_ein"
    verification = data.get("verification") or {}
    verification_status = verification.get(
        "status",
    )  # pending, verified, unverified, unavailable

    if not customer_id:
        logger.warning(
            {
                "message": "Tax ID event missing customer_id",
                "event_id": event_id,
            },
        )
        session.commit()
        return Response(status_code=200)

    # Find billing account by Stripe customer ID
    ba = session.query(BillingAccount).filter_by(stripe_customer_id=customer_id).first()

    if ba:
        if event["type"] == "customer.tax_id.created":
            ba.tax_id = tax_id_value
            if tax_id_type:
                ba.tax_id_type = tax_id_type
            if verification_status:
                ba.tax_id_verification_status = verification_status
            logger.info(
                {
                    "message": "BillingAccount tax ID synced from Stripe",
                    "billing_account_id": ba.id,
                    "event_type": event["type"],
                    "verification_status": verification_status,
                },
            )
        elif event["type"] == "customer.tax_id.deleted":
            ba.tax_id = None
            ba.tax_id_type = None
            ba.tax_id_verification_status = None
            logger.info(
                {
                    "message": "BillingAccount tax ID cleared from Stripe deletion",
                    "billing_account_id": ba.id,
                },
            )
        elif event["type"] == "customer.tax_id.updated":
            ba.tax_id = tax_id_value
            if tax_id_type:
                ba.tax_id_type = tax_id_type
            if verification_status:
                ba.tax_id_verification_status = verification_status
            logger.info(
                {
                    "message": "BillingAccount tax ID updated from Stripe",
                    "billing_account_id": ba.id,
                    "verification_status": verification_status,
                },
            )
    else:
        logger.warning(
            {
                "message": "Tax ID event for unknown Stripe customer",
                "customer_id": customer_id,
                "event_id": event_id,
            },
        )

    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def process_customer_updated_event(event: Dict, session: Session) -> Response:
    """
    Handler for customer.updated events from Stripe.

    Syncs customer details (email, name, address) changed via the Stripe
    dashboard back to the corresponding BillingAccount.
    """
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

    customer_id = data.get("id")
    if not customer_id:
        session.commit()
        return Response(status_code=200)

    # Find billing account by Stripe customer ID
    ba = session.query(BillingAccount).filter_by(stripe_customer_id=customer_id).first()
    if not ba:
        logger.debug(
            {
                "message": "customer.updated for unknown Stripe customer",
                "customer_id": customer_id,
                "event_id": event_id,
            },
        )
        session.commit()
        return Response(status_code=200)

    # Use `previous_attributes` to only sync fields that actually changed
    previous = event.get("data", {}).get("previous_attributes", {})
    changed = False

    if "email" in previous:
        ba.billing_email = data.get("email")
        changed = True

    if "name" in previous:
        ba.name = data.get("name")
        changed = True

    if "address" in previous:
        stripe_address = data.get("address")
        if stripe_address:
            ba.billing_address = {
                "line1": stripe_address.get("line1") or "",
                "line2": stripe_address.get("line2") or "",
                "city": stripe_address.get("city") or "",
                "state": stripe_address.get("state") or "",
                "postal_code": stripe_address.get("postal_code") or "",
                "country": stripe_address.get("country") or "",
            }
        else:
            ba.billing_address = None
        changed = True

    if "tax_exempt" in previous:
        # Log but don't override — tax_exempt is managed by our tax ID sync
        logger.info(
            {
                "message": "Stripe tax_exempt changed (info only, not synced back)",
                "billing_account_id": ba.id,
                "new_value": data.get("tax_exempt"),
            },
        )

    if changed:
        logger.info(
            {
                "message": "BillingAccount synced from Stripe customer.updated",
                "billing_account_id": ba.id,
                "changed_fields": list(previous.keys()),
            },
        )

    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def handle_event_core(event: Dict, session: Session) -> Response:  # noqa: D401
    """Main dispatcher for all Stripe webhook events."""
    event_type = event.get("type", "")
    if event_type.startswith("checkout.session."):
        return process_checkout_session_event(event, session)
    elif event_type.startswith("invoice."):
        return process_invoice_event(event, session)
    elif event_type.startswith("review."):
        return process_review_event(event, session)
    elif event_type.startswith("charge."):
        return process_charge_event(event, session)
    elif event_type.startswith("customer.tax_id."):
        return process_customer_tax_id_event(event, session)
    elif event_type == "customer.updated":
        return process_customer_updated_event(event, session)
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
async def handle_stripe_webhook(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Handle Stripe webhook events to update user credits based on payment outcomes."""
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    # Configure Stripe API key
    from orchestra.lib.billing import configure_stripe

    try:
        configure_stripe()
    except RuntimeError:
        logger.error({"message": "stripe_secret_key not configured in settings"})
        raise HTTPException(status_code=500, detail="Stripe configuration error")

    # For local development, allow skipping signature verification
    SKIP_SIGNATURE_VERIFICATION = settings.stripe_skip_signature_verification

    if SKIP_SIGNATURE_VERIFICATION:
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
        if not settings.stripe_webhook_secret:
            logger.error(
                {
                    "message": "stripe_webhook_secret not configured, but required for signature verification",
                },
            )
            raise HTTPException(
                status_code=500,
                detail="Stripe configuration error: Missing webhook secret",
            )

        # Production mode - verify signature
        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=settings.stripe_webhook_secret,
                tolerance=600,
            )
        except ValueError as e:
            logger.error({"message": "Invalid payload", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.SignatureVerificationError as e:
            logger.error({"message": "Signature verification failed", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid signature")

    # Process all events using the DI-provided session
    return handle_event_core(event, session)
