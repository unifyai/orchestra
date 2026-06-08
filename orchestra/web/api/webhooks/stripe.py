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
from datetime import datetime, timezone
from typing import Dict

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.webhook_log_dao import WebhookLogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    BillingAccount,
    Recharge,
    RechargeStatus,
    User,
    WebhookLog,
)
from orchestra.observability.prometheus_middleware import (
    INVOICE_FAILED_TOTAL,
    INVOICE_PAID_TOTAL,
)
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────
# Helpers
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


def _invoice_subscription_id(invoice: Dict) -> Optional[str]:
    """Subscription id behind an invoice, across Stripe API versions.

    The top-level ``invoice.subscription`` field was removed in the current
    Stripe API ("basil", 2025-05-28); the id now lives at
    ``invoice.parent.subscription_details.subscription``. Check the legacy
    location first (older API versions / unit-test fixtures) then the new
    one. Returns ``None`` for a non-subscription invoice.
    """
    sub = invoice.get("subscription")
    if not sub:
        sub = (
            (invoice.get("parent") or {}).get("subscription_details") or {}
        ).get("subscription")
    if isinstance(sub, dict):
        return sub.get("id")
    return sub or None


def _resolve_ba_for_subscription(
    session: Session,
    data: Dict,
) -> BillingAccount | None:
    """Resolve the billing account behind a subscription-linked object.

    Prefers the Stripe customer id (always present on invoices and
    subscriptions); falls back to the ``billing_account_id`` stamped
    into the subscription metadata at create time.
    """
    customer_id = data.get("customer")
    if customer_id:
        ba = BillingAccountDAO(session).get_by_stripe_customer_id(customer_id)
        if ba is not None:
            return ba

    # Metadata fallback — invoices expose subscription metadata under
    # ``subscription_details.metadata`` (legacy) or
    # ``parent.subscription_details.metadata`` (basil, 2025-05-28+);
    # subscription objects under ``metadata``.
    meta = (
        (data.get("subscription_details") or {}).get("metadata")
        or (
            (data.get("parent") or {}).get("subscription_details") or {}
        ).get("metadata")
        or data.get("metadata")
        or {}
    )
    ba_id = meta.get("billing_account_id")
    if ba_id:
        try:
            return session.get(BillingAccount, int(ba_id))
        except (ValueError, TypeError):
            return None
    return None


def _process_subscription_invoice(
    event: Dict,
    session: Session,
    data: Dict,
) -> Response:
    """Handle ``invoice.*`` events for self-serve subscription invoices.

    * ``invoice.paid`` (``subscription_create`` / ``subscription_cycle``):
      forfeit the prior plan grant remainder, grant the tier's monthly
      credits, record a PAID ``monthly_commit`` Recharge, reset the
      anniversary (the active assignment's anchor already moved on
      subscribe/upgrade). Proration invoices (``subscription_update``)
      are no-ops here — the upgrade endpoint grants the delta inline.
    * ``invoice.payment_failed`` / ``invoice.payment_action_required``:
      flag the account ``past_due`` as a *soft* warning but keep it
      ``ACTIVE`` so service is not cut off during Stripe's multi-week
      dunning/retry window. The account is only hard-``SUSPENDED`` once
      Stripe gives up — ``customer.subscription.updated`` with
      ``status=unpaid`` (see :func:`process_subscription_event`). A
      recovered payment (``invoice.paid``) clears the flag.
    """
    from orchestra.lib.subscription_billing import (
        apply_subscription_invoice_paid,
        record_proration_invoice,
    )

    event_type = event["type"]
    invoice_id = data.get("id")
    billing_reason = data.get("billing_reason")

    ba = _resolve_ba_for_subscription(session, data)
    if ba is None:
        logger.warning(
            {
                "message": "Subscription invoice for unknown billing account",
                "invoice_id": invoice_id,
                "stripe_customer_id": data.get("customer"),
            },
        )
        session.commit()
        return Response(status_code=200)

    if event_type == "invoice.paid":
        if billing_reason in ("subscription_create", "subscription_cycle"):
            apply_subscription_invoice_paid(session, ba, data)
            AssistantContactDAO(session).maybe_clear_grace_period(ba)
            session.commit()
            INVOICE_PAID_TOTAL.labels(billing_account_id=str(ba.id)).inc()
            return Response(status_code=200)
        if billing_reason == "subscription_update":
            # Mid-cycle tier-change proration. The credit delta was already
            # granted inline by the upgrade endpoint; record the charge so the
            # in-app invoice list reconciles with Stripe (wallet untouched).
            record_proration_invoice(session, ba, data)
            session.commit()
            return Response(status_code=200)
        # Any other billing reason: acknowledge without state change.
        session.commit()
        return Response(status_code=200)

    if event_type in (
        "invoice.payment_failed",
        "invoice.payment_action_required",
    ):
        # Soft past-due: flag it but DON'T suspend yet. Stripe retries the
        # charge for ~2-3 weeks (smart retries) and emails the customer;
        # cutting service off on the first failure is too aggressive. The
        # hard suspend happens only when Stripe marks the subscription
        # ``unpaid`` (retries exhausted) in ``customer.subscription.updated``.
        # We record the delinquency on ``payment_past_due_at`` (not
        # ``suspension_reason``) so the account stays cleanly ACTIVE — the
        # reason field keeps its "why suspended" meaning. Stamp only the first
        # failure of a streak so the timestamp marks when dunning began.
        if ba.payment_past_due_at is None:
            ba.payment_past_due_at = datetime.now(timezone.utc)
        session.commit()
        INVOICE_FAILED_TOTAL.labels(billing_account_id=str(ba.id)).inc()
        logger.info(
            {
                "message": "Subscription invoice payment failed — past due (soft)",
                "invoice_id": invoice_id,
                "billing_account_id": ba.id,
            },
        )
        return Response(status_code=200)

    # invoice.payment_succeeded (grants happen on invoice.paid),
    # invoice.finalized, etc. — acknowledged, no state change.
    session.commit()
    return Response(status_code=200)


def process_subscription_event(event: Dict, session: Session) -> Response:
    """Handle ``customer.subscription.*`` lifecycle events.

    * ``customer.subscription.updated``: keep the local plan assignment
      in sync when the quantity/tier is changed out of band (e.g. via
      the Stripe dashboard). The quantity maps 1:1 back to a tier
      template. No credit grant here — credits move on ``invoice.paid``.
    * ``customer.subscription.deleted``: end the tier assignment, revert
      to the free/default template, forfeit any remaining plan credits,
      and clear ``stripe_subscription_id``.
    * ``customer.subscription.updated`` with ``status=incomplete_expired``:
      a new subscription whose first payment never completed. The tier was
      never activated locally (activation is deferred to ``invoice.paid``),
      so this is mostly a no-op — but we still run the revert defensively to
      clear the dead ``stripe_subscription_id`` so a re-subscribe is clean.

    Dunning escalation: when Stripe exhausts its retries it flips the
    subscription to ``status=unpaid`` (delivered here as
    ``customer.subscription.updated``). That is the point at which we
    hard-``SUSPEND`` the account — not the first failed invoice. A return
    to ``status=active`` (payment recovered out of band) clears a
    past-due hold.
    """
    from orchestra.lib.subscription_billing import (
        find_tier_template_by_quantity,
        is_self_serve_sub_tier,
        revert_to_default_on_cancel,
    )

    data = event["data"]["object"]
    event_id: str = event["id"]
    event_type: str = event["type"]

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

    ba = _resolve_ba_for_subscription(session, data)
    if ba is None:
        logger.warning(
            {
                "message": "Subscription event for unknown billing account",
                "event_type": event_type,
                "stripe_customer_id": data.get("customer"),
            },
        )
        session.commit()
        return Response(status_code=200)

    if event_type == "customer.subscription.deleted":
        revert_to_default_on_cancel(session, ba)
        session.commit()
        return Response(status_code=200)

    if event_type == "customer.subscription.updated":
        # Dunning escalation / recovery driven by the Stripe subscription
        # status (the per-invoice failure only sets a soft past-due flag).
        sub_status = data.get("status")
        if sub_status == "incomplete_expired":
            # A brand-new subscription whose first payment was never
            # completed: Stripe holds it ``incomplete`` for ~23h then
            # expires it. No ``invoice.paid`` ever fired, so the tier was
            # never activated locally (it's deferred to first payment) and
            # no credits were granted. Run the revert defensively to clear
            # the dead subscription id so a re-subscribe starts clean.
            revert_to_default_on_cancel(session, ba)
            logger.info(
                {
                    "message": "Subscription incomplete_expired — reverted to free",
                    "billing_account_id": ba.id,
                },
            )
            session.commit()
            return Response(status_code=200)
        if sub_status == "unpaid":
            # Stripe exhausted retries — now we stop service. The soft
            # delinquency window is over; escalate to a hard suspension.
            ba.account_status = "SUSPENDED"
            ba.suspension_reason = "past_due"
            if ba.payment_past_due_at is None:
                ba.payment_past_due_at = datetime.now(timezone.utc)
            logger.info(
                {
                    "message": "Subscription unpaid (retries exhausted) — suspended",
                    "billing_account_id": ba.id,
                },
            )
        elif sub_status == "active" and (
            ba.suspension_reason == "past_due" or ba.payment_past_due_at is not None
        ):
            # Payment recovered out of band — lift both the soft delinquency
            # marker and, if we'd escalated to a hard past-due suspension, the
            # suspension itself.
            if ba.suspension_reason == "past_due":
                ba.account_status = "ACTIVE"
                ba.suspension_reason = None
            ba.payment_past_due_at = None

        # Keep the next-renewal mirror fresh (period end can move when the
        # plan/quantity changes or the cycle rolls). ``current_period_end``
        # was removed from the subscription top-level in the basil API
        # (2025-05-28) and now lives per-item; check both.
        period_end = data.get("current_period_end")
        if not period_end:
            _items = (data.get("items") or {}).get("data") or []
            period_end = _items[0].get("current_period_end") if _items else None
        if period_end:
            try:
                ba.current_period_end = datetime.fromtimestamp(
                    int(period_end),
                    tz=timezone.utc,
                )
            except (ValueError, TypeError, OverflowError):
                pass

        # Mirror the scheduled-cancellation flag so the console's persistent
        # "cancels on X" indicator stays correct for both in-app and
        # Portal/Dashboard cancels (and clears if the cancel is undone).
        ba.subscription_cancel_at_period_end = bool(
            data.get("cancel_at_period_end"),
        )

        # Sync the tier ONLY for an out-of-band change to an *already-active*
        # subscription (e.g. a quantity edit in the Stripe dashboard). The
        # INITIAL free→paid activation — and its credit grant — is owned
        # exclusively by ``invoice.paid`` (subscription_create). Doing it here
        # too would race that handler: both call ``set_plan`` under the
        # single-active-assignment unique index, and if this one wins the
        # account ends up on the paid tier with NO credits granted (no grant
        # happens here). Gating on "current plan is already a self-serve
        # sub tier" means we skip during initial activation (current plan is
        # still the free/default template) and let invoice.paid do it.
        items = (data.get("items") or {}).get("data") or []
        quantity = items[0].get("quantity") if items else None
        if quantity and sub_status != "incomplete":
            from orchestra.db.dao.billing_plan_assignment_dao import (
                BillingPlanAssignmentDAO,
            )
            from orchestra.db.models.orchestra_models import BillingPlanTemplate

            plan_dao = BillingPlanAssignmentDAO(session)
            current = plan_dao.resolve_effective_plan(ba.id)
            current_template = session.get(
                BillingPlanTemplate,
                current.template_id,
            )
            if is_self_serve_sub_tier(current_template):
                # Disambiguate the tier rung by billing interval: a monthly
                # tier and its annual sibling share the same quantity, so match
                # on the price's recurring interval too (year => annual).
                price = (items[0].get("price") or {}) if items else {}
                recurring = price.get("recurring") or {}
                annual = recurring.get("interval") == "year"
                template = find_tier_template_by_quantity(
                    session,
                    int(quantity),
                    annual=annual,
                )
                if template is not None and current.template_id != template.id:
                    try:
                        plan_dao.set_plan(
                            billing_account_id=ba.id,
                            template_id=template.id,
                            change_reason=(
                                "sync from Stripe subscription.updated "
                                f"(quantity={quantity})"
                            ),
                            effective_at=datetime.now(timezone.utc),
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            {
                                "message": "Failed to sync plan from subscription.updated",
                                "billing_account_id": ba.id,
                                "quantity": quantity,
                            },
                        )
        session.commit()
        return Response(status_code=200)

    session.commit()
    return Response(status_code=200)


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

    # Self-serve subscription invoices (the Stripe Subscription is the
    # collection engine for CREDITS tier plans) are handled on a
    # dedicated path: ``invoice.paid`` grants the monthly credits, a
    # failure marks the account past-due. They carry a subscription id
    # (top-level pre-basil, under ``parent.subscription_details`` from the
    # 2025-05-28 API on); the legacy credits/metered monthly-invoicer rows
    # never do, so this routing cleanly separates the two worlds.
    if _invoice_subscription_id(data):
        return _process_subscription_invoice(event, session, data)

    invoice_metadata = data.get("metadata", {})
    recharges = _resolve_recharges_for_invoice(
        session,
        invoice_id,
        invoice_metadata,
    )

    billing_account_ids = {r.billing_account_id for r in recharges}

    # ── success ──────────────────────────────────────────────────────────
    if event["type"] == "invoice.payment_succeeded":
        (
            session.query(Recharge)
            .filter_by(stripe_invoice_id=invoice_id)
            .update({"status": RechargeStatus.PAID}, synchronize_session=False)
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
        final = data["status"] in ("past_due", "uncollectible")
        if final:
            # Record the collection failure on the recharge rows. For the
            # metered monthly invoicer this is a bookkeeping signal only:
            # the invoice represents real usage already incurred, so it is
            # left outstanding in Stripe for collection/retry — we do not
            # void credits or auto-void the invoice here.
            (
                session.query(Recharge)
                .filter_by(stripe_invoice_id=invoice_id)
                .update({"status": RechargeStatus.FAILED}, synchronize_session=False)
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
            try:
                from orchestra.routines.billing_notifications import (
                    notify_billing_event_failure,
                )

                notify_billing_event_failure(
                    "webhook_refund",
                    error=str(e),
                    context_id=event_id,
                )
            except Exception:
                logger.warning(
                    "Failed to send billing event notification",
                    exc_info=True,
                )
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
                # ``deduct_credits`` is mode-aware: CREDITS debits the
                # wallet (legacy behaviour); METERED writes a ledger-only
                # audit row.
                billing_account_dao.deduct_credits(
                    ba.id,
                    credits_to_remove,
                    category="refund",
                    description="Credits removed due to refund",
                    detail={
                        "event": "refund",
                        "payment_intent_id": payment_intent_id,
                        "refund_fraction": fraction,
                    },
                )
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
            ba.suspension_reason = "dispute"

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
                # ``deduct_credits`` dispatches on billing-mode: CREDITS
                # accounts get the wallet debited (legacy behaviour);
                # METERED accounts get the wallet untouched. Both
                # produce the same audit trail in CreditTransaction.
                billing_account_dao.deduct_credits(
                    ba.id,
                    credits_original,
                    category="dispute",
                    description="Credits removed due to dispute",
                    detail={
                        "event": "dispute",
                        "payment_intent_id": payment_intent_id,
                    },
                )
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

                session.query(Recharge).filter_by(stripe_invoice_id=invoice_id).update(
                    {"status": RechargeStatus.DISPUTED},
                    synchronize_session=False,
                )

                # Same mode-aware branch as the metadata path above:
                # CREDITS gets a wallet debit; METERED gets an audit
                # ledger row only.
                billing_account_dao.deduct_credits(
                    ba_id,
                    total_credits,
                    category="dispute",
                    description="Credits removed due to dispute (invoice)",
                    detail={
                        "event": "dispute",
                        "invoice_id": invoice_id,
                    },
                )

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
                # ``add_credits`` is mode-aware: CREDITS restores the
                # wallet balance; METERED writes a ledger-only audit
                # credit. We don't expect this branch to fire on METERED
                # in practice (no cardholder disputes on invoiced
                # contracts) but the uniform path keeps the audit trail
                # consistent.
                billing_account_dao.add_credits(
                    ba.id,
                    credits_original,
                    category="dispute",
                    description="Credits restored — dispute won",
                    detail={
                        "event": "dispute_won",
                        "payment_intent_id": payment_intent_id,
                    },
                )

                if invoice_id:
                    session.query(Recharge).filter_by(
                        stripe_invoice_id=invoice_id,
                        status=RechargeStatus.DISPUTED,
                    ).update(
                        {"status": RechargeStatus.PAID},
                        synchronize_session=False,
                    )

                has_other_disputes = (
                    session.query(Recharge)
                    .filter(
                        Recharge.billing_account_id == ba.id,
                        Recharge.status == RechargeStatus.DISPUTED,
                    )
                    .first()
                    is not None
                )
                if ba.account_status == "SUSPENDED" and not has_other_disputes:
                    if (
                        ba.suspension_reason == "dispute"
                        or ba.suspension_reason is None
                    ):
                        ba.account_status = "ACTIVE"
                        ba.suspension_reason = None

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
                    has_other_disputes = (
                        session.query(Recharge)
                        .filter(
                            Recharge.billing_account_id == ba.id,
                            Recharge.status == RechargeStatus.DISPUTED,
                        )
                        .first()
                        is not None
                    )
                    if not has_other_disputes:
                        if (
                            ba.suspension_reason == "dispute"
                            or ba.suspension_reason is None
                        ):
                            ba.account_status = "ACTIVE"
                            ba.suspension_reason = None
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
        # PII (the tax ID value/type) is no longer mirrored locally — it lives
        # only on the Stripe Customer. We maintain just the derived
        # ``is_business`` flag: a present tax ID flips it on unless Stripe
        # explicitly reports the ID ``unverified``; deletion flips it off.
        if event["type"] in ("customer.tax_id.created", "customer.tax_id.updated"):
            ba.is_business = bool(tax_id_value) and verification_status != "unverified"
            logger.info(
                {
                    "message": "BillingAccount is_business derived from Stripe tax ID",
                    "billing_account_id": ba.id,
                    "event_type": event["type"],
                    "verification_status": verification_status,
                    "is_business": ba.is_business,
                },
            )
        elif event["type"] == "customer.tax_id.deleted":
            ba.is_business = False
            logger.info(
                {
                    "message": "BillingAccount is_business cleared (Stripe tax ID deleted)",
                    "billing_account_id": ba.id,
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

    # PII (email / name / address) is no longer mirrored back from Stripe —
    # Stripe is the source of truth and the profile screen reads it live, so
    # there's nothing to reconcile here. The one thing we keep in sync is the
    # *derived* (non-PII) ``billing_setup_complete`` gate: if the address is
    # edited outside our PATCH endpoint (e.g. in the Stripe dashboard), recompute
    # the boolean from the event's current address so the subscribe gate can't
    # go stale. We store only the boolean, never the address.
    from orchestra.lib.billing import is_billing_address_complete

    new_setup_complete = is_billing_address_complete(data.get("address"))
    if bool(ba.billing_setup_complete) != new_setup_complete:
        ba.billing_setup_complete = new_setup_complete
        logger.info(
            {
                "message": "Refreshed billing_setup_complete from customer.updated",
                "billing_account_id": ba.id,
                "billing_setup_complete": new_setup_complete,
            },
        )

    previous = event.get("data", {}).get("previous_attributes", {})

    if "tax_exempt" in previous:
        # Log but don't override — tax_exempt is managed by our tax ID sync
        logger.info(
            {
                "message": "Stripe tax_exempt changed (info only, not synced back)",
                "billing_account_id": ba.id,
                "new_value": data.get("tax_exempt"),
            },
        )

    session.commit()
    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def process_cash_balance_transaction_event(
    event: Dict,
    session: Session,
) -> Response:
    """Observability-only handler for ``customer_cash_balance_transaction.*``.

    Fires when a wire transfer from a customer using ``customer_balance``
    lands in their Stripe-issued virtual account. We deliberately do
    NOT reconcile the recharge here — Stripe will (assuming
    *Settings → Billing → Customer Balance → Apply unapplied funds*
    is on) auto-apply the new balance to the open invoice and emit
    ``invoice.payment_succeeded``, which the existing handler picks
    up and marks the recharge ``PAID``.

    What we do is log:

    * **funded**: a wire arrived. Useful breadcrumb for "did it land?"
      questions from the customer; correlatable by Stripe customer id.
    * **applied_to_payment**: the auto-apply happened. If we don't
      see ``invoice.payment_succeeded`` shortly after, that's the
      signal the auto-apply setting is off.
    * **unapplied_from_payment** / **adjusted_for_overdraft**: edge
      cases (overpayment, refund). Surface to ops via WARNING so they
      can decide whether to refund the surplus or leave it as a credit
      on the next invoice.

    All branches return 200 to keep Stripe from retrying — this event
    is informational and never a failure.
    """
    data = event.get("data", {}).get("object", {})
    customer_id = data.get("customer")
    txn_type = data.get("type")
    net_amount = data.get("net_amount")
    currency = data.get("currency")
    ending_balance = data.get("ending_balance")

    ba = (
        session.query(BillingAccount).filter_by(stripe_customer_id=customer_id).first()
        if customer_id
        else None
    )
    ba_id = ba.id if ba else None

    payload = {
        "message": "Stripe cash_balance_transaction received",
        "event_type": event.get("type"),
        "stripe_customer_id": customer_id,
        "billing_account_id": ba_id,
        "txn_type": txn_type,
        "net_amount": net_amount,
        "currency": currency,
        "ending_balance": ending_balance,
    }

    if txn_type in ("unapplied_from_payment", "adjusted_for_overdraft"):
        # Customer overpaid or Stripe applied a balance correction —
        # leaves an unapplied balance that won't auto-clear without
        # an open invoice in the matching currency. Ops decision:
        # refund or carry forward.
        logger.warning(payload)
    else:
        logger.info(payload)

    return Response(status_code=200)


# ──────────────────────────────────────────────────────────────────────────
def handle_event_core(event: Dict, session: Session) -> Response:  # noqa: D401
    """Main dispatcher for all Stripe webhook events."""
    event_type = event.get("type", "")
    if event_type.startswith("invoice."):
        return process_invoice_event(event, session)
    elif event_type.startswith("review."):
        return process_review_event(event, session)
    elif event_type.startswith("charge."):
        return process_charge_event(event, session)
    elif event_type.startswith("customer.subscription."):
        return process_subscription_event(event, session)
    elif event_type.startswith("customer.tax_id."):
        return process_customer_tax_id_event(event, session)
    elif event_type == "customer.updated":
        return process_customer_updated_event(event, session)
    elif event_type.startswith("customer_cash_balance_transaction."):
        # Wire-transfer (customer_balance) lifecycle events. Info-only —
        # the recharge → PAID transition still flows through
        # invoice.payment_succeeded once Stripe auto-applies the
        # received balance to the open invoice.
        # Note: Stripe names this event with underscores
        # (``customer_cash_balance_transaction``) rather than the dotted
        # ``customer.*`` pattern used by adjacent customer events. The
        # ``cash_balance.funds_available`` event is *separate* (sibling
        # event, fires on positive remaining balance) and is not
        # currently subscribed.
        return process_cash_balance_transaction_event(event, session)
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
                tolerance=300,
            )
        except ValueError as e:
            logger.error({"message": "Invalid payload", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.SignatureVerificationError as e:
            logger.error({"message": "Signature verification failed", "error": str(e)})
            raise HTTPException(status_code=400, detail="Invalid signature")

    # Process all events using the DI-provided session
    return handle_event_core(event, session)
