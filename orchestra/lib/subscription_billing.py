"""Self-serve subscription lifecycle orchestration.

Shared between the subscribe endpoint (``web/api/billing/views.py``) and
the Stripe webhook handler (``web/api/webhooks/stripe.py``). The Stripe
Subscription is the *collection engine*; the orchestra plan template is
the source of truth for the monthly credit grant. At 1 credit = $1 the
subscription quantity == the tier's ``commit_amount`` == the monthly
credit grant.

Key invariants enforced here:

* Only ``CREDITS`` / ``STRIPE_SUBSCRIPTION`` templates are subscribable
  (METERED enterprise billing is never touched).
* Billing is anniversary-anchored: the active ``BillingPlanAssignment``
  ``started_at`` is the anchor; the monthly plan grant expires at the
  next cycle (``invoice`` period end, or +1 month as a fallback).
* On each paid cycle the prior plan grant remainder is force-forfeited
  (credits reset) before the fresh grant is posted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
from orchestra.db.models.enums import CollectionMethod
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_MONTHLY_COMMIT,
    RECHARGE_TYPE_PRORATION,
    BillingAccount,
    BillingMode,
    BillingPlanTemplate,
    Recharge,
    RechargeStatus,
)
from orchestra.lib.billing import (
    PaymentMethodError,
    configure_stripe,
    ensure_stripe_customer,
    resolve_default_payment_method,
)
from orchestra.lib.credit_grants import (
    GRANT_KIND_PLAN,
    add_one_month,
    add_one_year,
    forfeit_plan_grant_remainder,
    format_display_credits,
    grant_expiring_credits,
    remaining_period_fraction,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    """Raised for self-serve subscription validation failures."""


# Annual tiers bill once a year and grant the whole year of credits up
# front (lump-sum bucket); the monthly tier rung is multiplied by this.
ANNUAL_MONTHS = 12


def tier_grant_quantity(template: BillingPlanTemplate) -> int:
    """The tier rung == Stripe subscription quantity == ``commit_amount``.

    This is the per-month credit unit (1 credit = $1), identical for a
    monthly tier and its annual sibling — the billing *interval* (monthly
    vs annual price) and the *credit grant* (1× vs 12×) are layered on
    independently, so money, credits and quantity stay decoupled.
    """
    if template.commit_amount is None:
        raise SubscriptionError(
            f"Template {template.id} ({template.name!r}) has no commit_amount; "
            "it cannot back a self-serve subscription.",
        )
    return int(Decimal(str(template.commit_amount)))


def is_annual(template: BillingPlanTemplate) -> bool:
    """Whether the tier bills annually (``commit_period == 'ANNUAL'``)."""
    return (template.commit_period or "").upper() == "ANNUAL"


def subscription_quantity(template: BillingPlanTemplate) -> int:
    """Stripe subscription item quantity for the tier (the rung == commit)."""
    return tier_grant_quantity(template)


def tier_credit_grant(template: BillingPlanTemplate) -> Decimal:
    """Credits granted per *paid invoice* for the tier.

    Monthly tiers grant ``commit_amount`` each cycle. Annual tiers bill
    once a year and grant the whole year as a single lump-sum bucket
    (``12 × commit_amount``) expiring at the annual period end — there is
    no separate monthly refresh. The annual discount is applied to the
    *price* (Stripe coupon), never to this grant.
    """
    base = Decimal(str(tier_grant_quantity(template)))
    return base * ANNUAL_MONTHS if is_annual(template) else base


def resolve_is_business(
    billing_account: BillingAccount,
    organization_id: Optional[int] = None,
) -> bool:
    """Whether to treat the account as a business for Stripe pricing/tax.

    Reads the locally-cached ``is_business`` flag. The flag is set when a
    tax ID is saved on the billing profile and refined by the
    ``customer.tax_id.*`` webhook (flipped off if Stripe reports the ID
    ``unverified``), so business tax treatment is still gated on a real,
    non-rejected tax ID — the difference is that the *value* now lives only
    in Stripe and we keep just the boolean locally (no PII to reconcile).

    Org membership alone is NOT sufficient — an org that hasn't supplied a
    tax ID is billed as an individual until it does.

    ``organization_id`` is retained for call-site compatibility but no
    longer affects the decision.

    Only consulted when *creating* a Customer / Subscription. Tier changes
    on an existing subscription keep the price already on the Stripe item
    (see :func:`_modify_subscription_quantity`), so a later profile edit can
    never flip an in-flight subscription's price.
    """
    return bool(billing_account.is_business)


def subscription_price_id(is_business: bool, *, annual: bool = False) -> str:
    """Resolve the recurring per-credit price id (personal/business × interval).

    Four prices total: {personal, business} × {monthly, annual}. The
    annual prices bill at $12/credit/year (== 12× the monthly rate); the
    discount rides on a coupon applied at subscribe time, not on the price.
    """
    if annual:
        price_id = (
            settings.stripe_unify_subscription_price_id_business_annual
            if is_business
            else settings.stripe_unify_subscription_price_id_personal_annual
        )
    else:
        price_id = (
            settings.stripe_unify_subscription_price_id_business_monthly
            if is_business
            else settings.stripe_unify_subscription_price_id_personal_monthly
        )
    if not price_id:
        ctx = "business" if is_business else "personal"
        interval = "annual" if annual else "monthly"
        raise SubscriptionError(
            f"Stripe subscription price ID not configured for {ctx} "
            f"{interval} workspace. Set the matching "
            "STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_* env var.",
        )
    return price_id


def is_self_serve_sub_tier(template: Optional[BillingPlanTemplate]) -> bool:
    """Whether ``template`` is a self-serve subscription tier (non-raising).

    The boolean counterpart to :func:`assert_self_serve_subscribable`: a
    CREDITS / STRIPE_SUBSCRIPTION template. Notably ``False`` for the
    free/default PAYG template, which is how callers tell "already on a
    paid tier" apart from "still on the free plan".
    """
    return (
        template is not None
        and template.billing_mode == BillingMode.CREDITS
        and template.collection_method == CollectionMethod.STRIPE_SUBSCRIPTION
    )


def assert_self_serve_subscribable(template: BillingPlanTemplate) -> None:
    """Guard: only CREDITS / STRIPE_SUBSCRIPTION tier templates qualify."""
    if (
        template.billing_mode != BillingMode.CREDITS
        or template.collection_method != CollectionMethod.STRIPE_SUBSCRIPTION
    ):
        raise SubscriptionError(
            f"Template {template.id} ({template.name!r}) is not a self-serve "
            "subscription tier (billing_mode must be CREDITS and "
            "collection_method STRIPE_SUBSCRIPTION).",
        )


def find_tier_template_by_quantity(
    session: Session,
    quantity: int,
    *,
    annual: bool = False,
) -> Optional[BillingPlanTemplate]:
    """Map a Stripe subscription quantity + interval back to its tier template.

    Used by the ``customer.subscription.updated`` webhook to keep the
    local plan assignment in sync when the quantity is changed out of
    band (e.g. via the Stripe dashboard). The tier rung (quantity) is
    shared between a monthly tier and its annual sibling, so the billing
    interval must also be matched to disambiguate.
    """
    period = "ANNUAL" if annual else "MONTHLY"
    return (
        session.execute(
            select(BillingPlanTemplate).where(
                BillingPlanTemplate.collection_method
                == CollectionMethod.STRIPE_SUBSCRIPTION,
                BillingPlanTemplate.billing_mode == BillingMode.CREDITS,
                BillingPlanTemplate.is_active.is_(True),
                BillingPlanTemplate.commit_amount == quantity,
                BillingPlanTemplate.commit_period == period,
            ),
        )
        .scalars()
        .first()
    )


def create_subscription(
    session: Session,
    billing_account: BillingAccount,
    template: BillingPlanTemplate,
    *,
    is_business: bool,
    user_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    fallback_email: Optional[str] = None,
    fallback_name: Optional[str] = None,
) -> stripe.Subscription:
    """Create the Stripe Subscription, charging the saved card off-session.

    Steps:
      1. Ensure a Stripe Customer exists (reuses ``ensure_stripe_customer``).
      2. Resolve the customer's default card (added up front in the in-app
         payment manager); refuse with ``PaymentMethodError`` if there's none.
      3. Create the Subscription (recurring per-credit price, quantity =
         tier grant) with that card as ``default_payment_method`` and
         ``payment_behavior="error_if_incomplete"`` so the first invoice is
         charged synchronously. A decline raises ``CardError`` and no
         subscription is persisted.
      4. Persist ``stripe_subscription_id`` on the account.

    The local ``BillingPlanAssignment`` is **not** activated here even though
    payment is collected synchronously: the tier is activated — and the
    credits granted — when Stripe confirms the charge via ``invoice.paid``
    (``billing_reason=subscription_create``), handled in
    :func:`apply_subscription_invoice_paid`. Keeping activation webhook-driven
    means there's a single source of truth for "this account is paid", and
    the brief gap is covered by the client polling for the subscription to
    become active right after subscribe.
    """
    assert_self_serve_subscribable(template)
    configure_stripe()

    quantity = subscription_quantity(template)
    annual = is_annual(template)

    customer_id = ensure_stripe_customer(
        session,
        billing_account,
        is_business=is_business,
        fallback_email=fallback_email,
        fallback_name=fallback_name,
    )

    # Self-serve subscribers add a card up front in the in-app payment
    # manager (a SetupIntent validated ``off_session``), so the first
    # invoice is charged off-session against that card rather than collected
    # on a Stripe-hosted page. Refuse early with a clear, actionable error
    # when there's no card to charge.
    default_payment_method = resolve_default_payment_method(customer_id)
    if not default_payment_method:
        raise PaymentMethodError(
            "Add a payment method before subscribing.",
        )

    metadata = {
        "template_id": str(template.id),
        "billing_account_id": str(billing_account.id),
        "billing_interval": "annual" if annual else "monthly",
    }

    create_kwargs: dict = {
        "customer": customer_id,
        "items": [
            {
                "price": subscription_price_id(is_business, annual=annual),
                "quantity": quantity,
            },
        ],
        "metadata": metadata,
        # Charge the first invoice synchronously against the saved card.
        # ``error_if_incomplete`` makes subscribe atomic with payment: a
        # decline (or an off-session SCA challenge we can't satisfy) raises
        # ``CardError`` and Stripe leaves no half-created subscription, so we
        # never persist a ``stripe_subscription_id`` for an unpaid account.
        "default_payment_method": default_payment_method,
        "payment_behavior": "error_if_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        # Stripe Tax computes VAT/sales tax on each invoice from the
        # customer's address (synced from the Billing Profile). The
        # subscribe endpoint gates on a billing address being present so
        # the location can always be resolved here; the prices are
        # tax-exclusive so tax is added on top of the per-credit rate.
        "automatic_tax": {"enabled": True},
        "expand": ["latest_invoice.payment_intent"],
    }
    # Annual tiers carry the discount as a coupon (price-only) so the
    # annual grant stays a clean 12× the monthly tier.
    coupon_id = settings.stripe_unify_annual_coupon_id
    if annual and coupon_id:
        create_kwargs["discounts"] = [{"coupon": coupon_id}]

    subscription = stripe.Subscription.create(**create_kwargs)

    subscription_id = (
        subscription.get("id") if isinstance(subscription, dict) else subscription.id
    )
    billing_account.stripe_subscription_id = subscription_id
    # A brand-new subscription is never scheduled to cancel (covers the
    # re-subscribe-after-cancel case where a stale flag could linger).
    billing_account.subscription_cancel_at_period_end = False
    session.flush()

    logger.info(
        {
            "message": "Self-serve subscription created (pending first payment)",
            "billing_account_id": billing_account.id,
            "template_id": template.id,
            "quantity": quantity,
            "stripe_subscription_id": subscription_id,
        },
    )
    return subscription


def _invoice_period_end(invoice: dict, *, annual: bool = False) -> datetime:
    """Best-effort cycle end from a Stripe invoice; +1 month/year fallback."""
    try:
        lines = invoice.get("lines", {}).get("data", [])
        if lines:
            period = lines[0].get("period", {})
            end = period.get("end")
            if end:
                return datetime.fromtimestamp(int(end), tz=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    return add_one_year(now) if annual else add_one_month(now)


def _invoice_line_is_annual(line: dict) -> bool:
    """Whether an invoice line bills annually.

    The recurring interval used to live at ``line.price.recurring.interval``,
    but the current Stripe API ("basil", 2025-05-28) dropped the expanded
    ``price`` from invoice lines — it's now ``line.pricing.price_details`` with
    only a price *id* and no interval. So fall back to the line's billing
    ``period`` span (a monthly rung covers ~1 month, an annual rung ~1 year),
    which is schema-independent. The legacy ``price.recurring`` path is kept
    for older API versions and the synthetic invoices used in unit tests.
    """
    recurring = (line.get("price") or {}).get("recurring") or {}
    interval = recurring.get("interval")
    if interval:
        return interval == "year"
    period = line.get("period") or {}
    start, end = period.get("start"), period.get("end")
    try:
        if start and end:
            # > ~2 months of coverage ⇒ an annual rung (monthly is ~1 month).
            return (int(end) - int(start)) > 60 * 24 * 60 * 60
    except (TypeError, ValueError):
        pass
    return False


def _tier_template_from_invoice(
    session: Session,
    invoice: dict,
) -> Optional[BillingPlanTemplate]:
    """Resolve the tier template from a subscription invoice's first line.

    Uses the line quantity (tier rung) + the billing interval (monthly vs
    annual, see :func:`_invoice_line_is_annual`) — the same mapping the
    ``subscription.updated`` webhook uses. Returns ``None`` when the line
    lacks a quantity (e.g. the synthetic invoices used in unit tests), in
    which case callers fall back to the already-active plan assignment.
    """
    try:
        line = (invoice.get("lines", {}).get("data", []) or [])[0]
    except (IndexError, AttributeError, TypeError):
        return None
    quantity = line.get("quantity")
    if not quantity:
        return None
    annual = _invoice_line_is_annual(line)
    try:
        return find_tier_template_by_quantity(session, int(quantity), annual=annual)
    except (TypeError, ValueError):
        return None


def apply_subscription_invoice_paid(
    session: Session,
    billing_account: BillingAccount,
    invoice: dict,
) -> Optional[Decimal]:
    """Activate the tier (first payment) then grant the period's credits.

    For a brand-new subscription the local plan assignment is *not*
    activated at subscribe time (see :func:`create_subscription`); it is
    activated here, when Stripe confirms the first payment, so an abandoned
    checkout never appears subscribed. Renewals already have the tier set,
    so the activation is a no-op for them and only the credit reset runs.

    Then: forfeit the prior plan grant remainder and grant the tier's
    period credits. Idempotency is handled upstream by the webhook log;
    this also short-circuits if a ``monthly_commit`` Recharge already
    exists for the same Stripe invoice (defensive against an
    ``invoice.paid`` + ``invoice.payment_succeeded`` double-delivery).

    Returns the new wallet balance, or ``None`` when skipped.
    """
    invoice_id = invoice.get("id")

    existing = (
        session.query(Recharge)
        .filter(
            Recharge.billing_account_id == billing_account.id,
            Recharge.type == RECHARGE_TYPE_MONTHLY_COMMIT,
            Recharge.stripe_invoice_id == invoice_id,
        )
        .first()
    )
    if existing is not None:
        return None

    plan_dao = BillingPlanAssignmentDAO(session)

    # Activate the tier on confirmed first payment. New subscriptions defer
    # activation until here; if the tier resolved from the invoice differs
    # from the currently active plan (i.e. still on free/default), switch to
    # it now (anniversary anchor = now). Renewals resolve to the same tier
    # so this is skipped, preserving the existing anchor.
    invoice_template = _tier_template_from_invoice(session, invoice)
    if invoice_template is not None:
        current = plan_dao.resolve_effective_plan(billing_account.id)
        if current.template_id != invoice_template.id:
            plan_dao.set_plan(
                billing_account_id=billing_account.id,
                template_id=invoice_template.id,
                change_reason=(
                    "self-serve subscription activated on first payment "
                    f"(invoice={invoice_id})"
                ),
                effective_at=datetime.now(timezone.utc),
            )
            session.flush()

    plan = plan_dao.resolve_effective_plan(billing_account.id)
    template = session.get(BillingPlanTemplate, plan.template_id)
    if template is None:
        return None
    try:
        assert_self_serve_subscribable(template)
    except SubscriptionError:
        # Active plan isn't a self-serve tier (e.g. a stray invoice for
        # an account that was moved to METERED) — leave the wallet alone.
        logger.warning(
            {
                "message": "Subscription invoice for non-tier plan; skipping grant",
                "billing_account_id": billing_account.id,
                "template_id": plan.template_id,
                "invoice_id": invoice_id,
            },
        )
        return None

    annual = is_annual(template)
    quantity = subscription_quantity(template)
    grant = tier_credit_grant(template)
    expires_at = _invoice_period_end(invoice, annual=annual)

    # Mirror the cycle end onto the account so the console can render the
    # next-renewal date (the plan grant expiry == the subscription period
    # end == the next credit reset).
    billing_account.current_period_end = expires_at

    # Credits reset each cycle: forfeit any unconsumed remainder of the
    # prior plan grant before posting the fresh one. (For annual tiers the
    # "cycle" is the year, so the prior annual bucket forfeits on renewal.)
    forfeit_plan_grant_remainder(session, billing_account)

    period_word = "annual" if annual else "monthly"
    new_balance = grant_expiring_credits(
        session,
        billing_account.id,
        grant,
        grant_kind=GRANT_KIND_PLAN,
        expires_at=expires_at,
        description=f"{period_word.capitalize()} plan credits ({template.name})",
    )

    # Reset the per-period upgrade high-water-mark to the full grant just
    # posted, so any mid-cycle upgrade now grants only what is above this
    # fresh baseline.
    billing_account.plan_credits_granted_period = grant

    # Record the collection as a PAID monthly_commit Recharge, attributed
    # to the active assignment so the invoice list + reconciliation see it.
    # ``amount_usd`` is the *actual* amount charged (post-coupon for annual)
    # read off the invoice; quantity stays the tier rung. They diverge for
    # annual (and any discounted) plans — that is the money/credits/quantity
    # decoupling in action.
    amount_paid = invoice.get("amount_paid")
    amount_usd = (
        Decimal(str(amount_paid)) / Decimal("100")
        if amount_paid is not None
        else Decimal(str(quantity))
    )
    recharge = Recharge(
        billing_account_id=billing_account.id,
        type=RECHARGE_TYPE_MONTHLY_COMMIT,
        quantity=Decimal(str(quantity)),
        amount_usd=amount_usd,
        status=RechargeStatus.PAID,
        stripe_invoice_id=invoice_id,
        plan_id=plan.assignment_id,
    )
    session.add(recharge)

    # Clear a past-due hold once payment recovers — both the soft delinquency
    # marker (ACTIVE + ``payment_past_due_at``) and a hard suspension
    # (SUSPENDED + ``suspension_reason='past_due'``).
    if billing_account.suspension_reason == "past_due":
        billing_account.account_status = "ACTIVE"
        billing_account.suspension_reason = None
    billing_account.payment_past_due_at = None

    session.flush()

    logger.info(
        {
            "message": "Subscription cycle credits granted",
            "billing_account_id": billing_account.id,
            "template_id": template.id,
            "billing_interval": period_word,
            "credits_granted": float(grant),
            "invoice_id": invoice_id,
            "billing_reason": invoice.get("billing_reason"),
        },
    )
    return new_balance


def record_proration_invoice(
    session: Session,
    billing_account: BillingAccount,
    invoice: dict,
) -> Optional[Recharge]:
    """Record a mid-cycle proration invoice as a PAID ``Recharge`` row.

    Stripe issues a separate ``billing_reason=subscription_update`` invoice
    for every in-place tier change (``proration_behavior='always_invoice'``):
    a prorated charge on an upgrade, a $0 invoice (with a customer-credit
    line) on a downgrade. The credit *delta* for the change is granted inline
    by :func:`change_plan`, so this does **not** touch the wallet — it exists
    purely so the in-app invoice list reconciles with what the customer sees
    in Stripe (otherwise only ``subscription_create``/``_cycle`` invoices show
    up). ``amount_usd`` is the actual amount charged read off the invoice.

    Idempotent on ``(type, stripe_invoice_id)`` as a defensive guard against
    ``invoice.paid`` + ``invoice.payment_succeeded`` double-delivery (the
    webhook log already dedupes by event id upstream).
    """
    invoice_id = invoice.get("id")

    existing = (
        session.query(Recharge)
        .filter(
            Recharge.billing_account_id == billing_account.id,
            Recharge.type == RECHARGE_TYPE_PRORATION,
            Recharge.stripe_invoice_id == invoice_id,
        )
        .first()
    )
    if existing is not None:
        return None

    plan = BillingPlanAssignmentDAO(session).resolve_effective_plan(
        billing_account.id,
    )

    amount_paid = invoice.get("amount_paid")
    amount_usd = (
        Decimal(str(amount_paid)) / Decimal("100")
        if amount_paid is not None
        else Decimal("0")
    )

    recharge = Recharge(
        billing_account_id=billing_account.id,
        type=RECHARGE_TYPE_PRORATION,
        # A proration isn't a credit-tier rung, so quantity is meaningless
        # here — the credits already moved inline on the tier change.
        quantity=Decimal("0"),
        amount_usd=amount_usd,
        status=RechargeStatus.PAID,
        stripe_invoice_id=invoice_id,
        plan_id=plan.assignment_id,
    )
    session.add(recharge)
    session.flush()

    logger.info(
        {
            "message": "Recorded subscription proration invoice",
            "billing_account_id": billing_account.id,
            "invoice_id": invoice_id,
            "amount_usd": float(amount_usd),
            "billing_reason": invoice.get("billing_reason"),
        },
    )
    return recharge


def cancel_subscription(
    session: Session,
    billing_account: BillingAccount,
    *,
    at_period_end: bool = True,
) -> Optional[datetime]:
    """Cancel the account's self-serve subscription via Stripe.

    Default (``at_period_end=True``): schedule cancellation at the end of
    the current period — the customer keeps their credits and service
    until then. Stripe emits ``customer.subscription.deleted`` at the
    boundary, which :func:`revert_to_default_on_cancel` handles (forfeit
    remainder + revert to the free tier).

    ``at_period_end=False``: cancel immediately; Stripe emits
    ``customer.subscription.deleted`` now and the webhook reverts the
    account (forfeiting any unconsumed credits) right away.

    Returns the effective cancellation time (period end) when scheduled,
    else ``None`` for an immediate cancel. The local plan state is NOT
    mutated here — the webhook is the single source of truth for the
    revert, keeping in-app cancel and dashboard/Portal cancel identical.
    """
    if not billing_account.stripe_subscription_id:
        raise SubscriptionError(
            f"BillingAccount {billing_account.id} has no active subscription "
            "to cancel.",
        )

    configure_stripe()
    if at_period_end:
        stripe.Subscription.modify(
            billing_account.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        # Reflect the scheduled cancellation locally right away so the console
        # shows a persistent indicator without waiting for the
        # ``customer.subscription.updated`` webhook to land (which keeps it in
        # sync afterwards, including for Portal/Dashboard cancels).
        billing_account.subscription_cancel_at_period_end = True
        session.flush()
        effective = billing_account.current_period_end
        logger.info(
            {
                "message": "Self-serve subscription set to cancel at period end",
                "billing_account_id": billing_account.id,
                "stripe_subscription_id": billing_account.stripe_subscription_id,
                "effective_at": effective.isoformat() if effective else None,
            },
        )
        return effective

    stripe.Subscription.delete(billing_account.stripe_subscription_id)
    logger.info(
        {
            "message": "Self-serve subscription cancelled immediately",
            "billing_account_id": billing_account.id,
            "stripe_subscription_id": billing_account.stripe_subscription_id,
        },
    )
    return None


def reactivate_subscription(
    session: Session,
    billing_account: BillingAccount,
) -> Optional[datetime]:
    """Undo a scheduled end-of-period cancellation.

    Clears Stripe's ``cancel_at_period_end`` flag so the subscription
    renews normally again, and reflects it locally right away (the
    ``customer.subscription.updated`` webhook keeps it in sync afterwards,
    including for Portal/Dashboard reactivations). Returns the next renewal
    time. Raises :class:`SubscriptionError` if there's no subscription to
    resume.

    Only valid while the subscription still exists (i.e. before Stripe has
    actually deleted it at the period boundary). Once deleted, the account
    has reverted to the free tier and the customer must subscribe afresh.
    """
    if not billing_account.stripe_subscription_id:
        raise SubscriptionError(
            f"BillingAccount {billing_account.id} has no active subscription "
            "to resume.",
        )

    configure_stripe()
    stripe.Subscription.modify(
        billing_account.stripe_subscription_id,
        cancel_at_period_end=False,
    )
    billing_account.subscription_cancel_at_period_end = False
    session.flush()
    effective = billing_account.current_period_end
    logger.info(
        {
            "message": "Self-serve subscription cancellation reversed",
            "billing_account_id": billing_account.id,
            "stripe_subscription_id": billing_account.stripe_subscription_id,
            "renews_at": effective.isoformat() if effective else None,
        },
    )
    return effective


def revert_to_default_on_cancel(
    session: Session,
    billing_account: BillingAccount,
) -> None:
    """End the tier assignment and revert to the free/default template.

    Called from ``customer.subscription.deleted``. Forfeits the
    unconsumed plan grant (credits don't survive an ended subscription)
    and clears ``stripe_subscription_id``.
    """
    from orchestra.db.models.orchestra_models import DEFAULT_TEMPLATE_ID

    forfeit_plan_grant_remainder(session, billing_account)

    plan_dao = BillingPlanAssignmentDAO(session)
    current = plan_dao.resolve_effective_plan(billing_account.id)
    if current.template_id != DEFAULT_TEMPLATE_ID:
        try:
            plan_dao.set_plan(
                billing_account_id=billing_account.id,
                template_id=DEFAULT_TEMPLATE_ID,
                change_reason="self-serve subscription cancelled (reverted to free)",
                effective_at=datetime.now(timezone.utc),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                {
                    "message": "Failed to revert to default template on cancel",
                    "billing_account_id": billing_account.id,
                },
            )

    billing_account.stripe_subscription_id = None
    billing_account.current_period_end = None
    billing_account.subscription_cancel_at_period_end = False
    billing_account.plan_credits_granted_period = Decimal(0)
    # A past-due hold no longer applies once the subscription is gone and
    # the account has dropped to the free tier — let them use it / re-subscribe.
    if billing_account.suspension_reason == "past_due":
        billing_account.account_status = "ACTIVE"
        billing_account.suspension_reason = None
    billing_account.payment_past_due_at = None
    session.flush()

    logger.info(
        {
            "message": "Self-serve subscription cancelled; reverted to default",
            "billing_account_id": billing_account.id,
        },
    )


# ============================================================================
# Tier changes (immediate upgrade/downgrade) + auto-increment-on-depletion
# ============================================================================


def _subscription_item_id(subscription: object) -> str:
    """Pull the (single) subscription item id from a Stripe Subscription."""
    items = (
        subscription.get("items")
        if isinstance(subscription, dict)
        else getattr(subscription, "items", None)
    ) or {}
    data = (items.get("data") if isinstance(items, dict) else items.data) or []
    if not data:
        raise SubscriptionError("Stripe subscription has no line items to update.")
    first = data[0]
    return first.get("id") if isinstance(first, dict) else first.id


def _modify_subscription_quantity(
    subscription_id: str,
    new_quantity: int,
    *,
    metadata: dict,
) -> None:
    """Re-point the subscription's single item to ``new_quantity`` credits.

    Only the *quantity* is changed — the item keeps whatever price it was
    created with (personal vs business is fixed at subscribe time), so a tier
    change can never accidentally flip pricing/tax for an in-flight
    subscription.

    Proration is invoiced immediately (``always_invoice``) so the customer
    is charged the incremental difference for the remainder of the current
    period right away — matching the immediate (non-AT_BOUNDARY) tier-change
    semantics of the self-serve model. The resulting ``invoice.paid`` carries
    ``billing_reason=subscription_update``, which the webhook treats as a
    no-op for credits (the delta is granted inline by the caller) but records
    a proration ``Recharge`` row for the invoice list.

    ``payment_behavior="error_if_incomplete"`` makes the change *atomic with
    payment*: Stripe attempts to settle the proration invoice synchronously
    and, if the card is declined (or needs SCA we can't complete
    off-session), raises a ``CardError`` and rolls the subscription back to
    its prior quantity. That keeps an upgrade from "succeeding" on a card
    that can't actually pay — the caller never reaches the local tier/credit
    grant because the exception propagates first.
    """
    configure_stripe()
    subscription = stripe.Subscription.retrieve(subscription_id)
    item_id = _subscription_item_id(subscription)
    stripe.Subscription.modify(
        subscription_id,
        items=[
            {
                "id": item_id,
                "quantity": new_quantity,
            },
        ],
        proration_behavior="always_invoice",
        payment_behavior="error_if_incomplete",
        metadata=metadata,
    )


def change_subscription_tier(
    session: Session,
    billing_account: BillingAccount,
    new_template: BillingPlanTemplate,
    *,
    user_id: Optional[str] = None,
) -> Decimal:
    """Move an *already-subscribed* account to another tier, immediately.

    Used by both the self-serve plan-switch endpoint and auto-increment.
    Mechanics:

      1. Update the Stripe subscription quantity (proration invoiced now).
         The existing item's *price* is kept untouched (personal vs business
         is fixed at subscribe time), so a tier change can never flip pricing.
      2. Re-anchor the cycle: ``set_plan(effective_at=now)`` so the
         anniversary restarts on the change date.
      3. **Upgrade**: grant the credits *above* the per-period
         high-water-mark (``plan_credits_granted_period``) **prorated to the
         remaining slice of the period** (so the immediate grant matches
         Stripe's prorated charge), then raise the mark to the full tier.
         **Downgrade**: no grant, the mark is untouched, and we never claw
         back already-consumed credits. This makes repeated upgrade/downgrade
         toggling non-mintable — you can never be granted more than the
         highest tier you reached this cycle, and auto-incrementing up the
         ladder only ever yields the prorated remainder of each rung.

    The full reset (forfeit-all-plan-lots + grant the new tier's full
    amount, mark reset to the new tier) still happens on the next
    ``subscription_cycle`` ``invoice.paid``. Returns the credit delta granted
    now (0 for downgrades, re-upgrades at/below the mark, or when no period
    slice remains).
    """
    assert_self_serve_subscribable(new_template)
    if not billing_account.stripe_subscription_id:
        raise SubscriptionError(
            f"BillingAccount {billing_account.id} has no active subscription; "
            "use create_subscription to subscribe first.",
        )

    plan_dao = BillingPlanAssignmentDAO(session)
    current = plan_dao.resolve_effective_plan(billing_account.id)
    old_template = session.get(BillingPlanTemplate, current.template_id)
    old_is_sub_tier = (
        old_template is not None
        and old_template.collection_method == CollectionMethod.STRIPE_SUBSCRIPTION
    )

    # Cross-interval (monthly↔annual) changes are not supported in place:
    # they would require swapping the Stripe price (and re-proration across
    # different cadences). The account cancels + resubscribes instead. The
    # available-plans list never offers a cross-interval target, so this is
    # only a defensive guard against a stale client.
    if old_is_sub_tier and is_annual(old_template) != is_annual(new_template):
        raise SubscriptionError(
            "Cannot switch between monthly and annual billing on an active "
            "subscription. Cancel the current plan and subscribe to the "
            "other interval.",
        )

    new_quantity = subscription_quantity(new_template)
    old_grant = tier_credit_grant(old_template) if old_is_sub_tier else Decimal(0)
    new_grant = tier_credit_grant(new_template)
    annual = is_annual(new_template)

    metadata = {
        "template_id": str(new_template.id),
        "billing_account_id": str(billing_account.id),
        "billing_interval": "annual" if annual else "monthly",
    }
    _modify_subscription_quantity(
        billing_account.stripe_subscription_id,
        new_quantity,
        metadata=metadata,
    )

    now = datetime.now(timezone.utc)
    plan_dao.set_plan(
        billing_account_id=billing_account.id,
        template_id=new_template.id,
        created_by_user_id=user_id,
        change_reason=(f"self-serve tier change {old_grant}->{new_grant} credits"),
        effective_at=now,
    )

    # Grant only what is above the per-period high-water-mark (in credits).
    # Defensive fallback to ``old_grant`` covers accounts predating the mark
    # column. The grant is *prorated* to the slice of the period still
    # remaining so the credits handed out now mirror Stripe's prorated
    # charge — otherwise an account could ride auto-increment up the ladder,
    # collecting each full tier delta while paying only the prorated stub,
    # and bank a year's worth of credits for the price of the lowest rung.
    # The remainder of the tier lands at the next cycle's ``invoice.paid``
    # (which forfeits + re-grants the full tier and resets the mark). The
    # high-water mark still advances to the full tier so a same-cycle
    # re-upgrade is never re-granted.
    granted_so_far = billing_account.plan_credits_granted_period or Decimal(0)
    high_water = max(Decimal(str(granted_so_far)), old_grant)
    full_delta = new_grant - high_water
    delta = Decimal("0")
    if full_delta > 0:
        period_end = billing_account.current_period_end
        expires_at = period_end or (add_one_year(now) if annual else add_one_month(now))
        fraction = remaining_period_fraction(period_end, now, annual=annual)
        # Floor to whole credits so we never over-grant relative to the
        # prorated charge; the rounding crumb settles at the next cycle.
        delta = (full_delta * fraction).quantize(Decimal("1"), rounding=ROUND_DOWN)
        if delta > 0:
            grant_expiring_credits(
                session,
                billing_account.id,
                delta,
                grant_kind=GRANT_KIND_PLAN,
                expires_at=expires_at,
                description=f"Tier upgrade delta credits ({new_template.name})",
            )
    billing_account.plan_credits_granted_period = max(high_water, new_grant)
    session.flush()

    logger.info(
        {
            "message": "Self-serve tier changed",
            "billing_account_id": billing_account.id,
            "from_template_id": current.template_id,
            "to_template_id": new_template.id,
            "billing_interval": "annual" if annual else "monthly",
            "old_grant": float(old_grant),
            "new_grant": float(new_grant),
            "full_delta": float(full_delta) if full_delta > 0 else 0.0,
            "high_water_mark": float(billing_account.plan_credits_granted_period),
            "delta_granted": float(delta),
        },
    )
    return delta


def next_tier_template(
    session: Session,
    billing_account: BillingAccount,
) -> Optional[BillingPlanTemplate]:
    """Return the next tier *up* the account's plan-group ladder, or None.

    The ladder is ``PlanGroupMember.position`` ascending within the
    account's ``plan_group_id``; the "next" tier is the active self-serve
    member with the smallest position strictly greater than the current
    plan's position **and the same billing interval** (auto-increment never
    flips a monthly plan onto an annual one). Returns ``None`` when the
    account is already on the top tier of its interval (or its current plan
    isn't an ordered group member).
    """
    from orchestra.db.dao.billing_plan_group_dao import BillingPlanGroupDAO

    plan_dao = BillingPlanAssignmentDAO(session)
    current = plan_dao.resolve_effective_plan(billing_account.id)
    current_template = session.get(BillingPlanTemplate, current.template_id)
    current_annual = is_annual(current_template) if current_template else False

    group_dao = BillingPlanGroupDAO(session)
    members = group_dao.list_members(
        billing_account.plan_group_id,
        include_inactive_templates=False,
    )
    current_position: Optional[int] = next(
        (m.position for m in members if m.template_id == current.template_id),
        None,
    )
    if current_position is None:
        return None

    for member in members:
        if member.position is None or member.position <= current_position:
            continue
        template = member.template
        if (
            template.billing_mode == BillingMode.CREDITS
            and template.collection_method == CollectionMethod.STRIPE_SUBSCRIPTION
            and is_annual(template) == current_annual
        ):
            return template
    return None


def build_auto_increment_email(new_template: BillingPlanTemplate) -> tuple[str, str]:
    """Subject + HTML body for the 'you were auto-upgraded' notification.

    Sent (best-effort) to the billing account holder when auto-increment
    bumps them to the next tier on depletion, since it is a charge they
    did not explicitly click.
    """
    credits = format_display_credits(tier_credit_grant(new_template))
    period_word = "year" if is_annual(new_template) else "month"
    subject = "Your Unify plan was automatically upgraded"
    body = (
        "<p>Heads up — your workspace ran out of credits this cycle and "
        "you have <strong>auto-increment</strong> enabled, so we moved you "
        f"up to the <strong>{new_template.name}</strong> plan "
        f"({credits} credits / {period_word}) to keep things running.</p>"
        "<p>A prorated charge for the difference was applied to your card "
        "for the remainder of the current billing period.</p>"
        "<p>If you'd rather not auto-upgrade in future, you can turn "
        "auto-increment off any time from your billing settings.</p>"
    )
    return subject, body


def auto_increment_on_depletion(
    session: Session,
    billing_account: BillingAccount,
) -> Optional[BillingPlanTemplate]:
    """Bump to the next tier when a depleted account has auto-increment on.

    Called right after a deduction drives the wallet to/below zero. No-op
    (returns ``None``) when auto-increment is off, the account isn't on a
    subscription, or it is already on the top tier (hard stop). Otherwise
    performs an immediate upgrade to the next tier (granting the delta) and
    returns the new tier template.
    """
    if not billing_account.auto_increment:
        return None
    if not billing_account.stripe_subscription_id:
        return None

    target = next_tier_template(session, billing_account)
    if target is None:
        logger.info(
            {
                "message": "Auto-increment requested but already at top tier",
                "billing_account_id": billing_account.id,
            },
        )
        return None

    change_subscription_tier(
        session,
        billing_account,
        target,
    )
    logger.info(
        {
            "message": "Auto-incremented to next tier on depletion",
            "billing_account_id": billing_account.id,
            "to_template_id": target.id,
        },
    )
    return target
