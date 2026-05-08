"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job runs once a month (on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total using
   the Stripe product
3. updates all rows to INVOICE_CREATED and stores the invoice-id

Recharges are grouped by billing_account_id (shared by User and
Organization). Each billing account is processed in its own savepoint
so that a Stripe failure for one account does not prevent the others
from being invoiced.

----------------------------------------------------------------------
Scheduling
----------------------------------------------------------------------

Production runs on **Google Cloud Scheduler**:

  * Job ``orchestra-production-monthly-invoicer`` in project
    ``saas-368716`` / location ``us-central1``.
  * Schedule ``0 2 1 * *`` UTC (02:00 on the 1st of each month).
  * POSTs to ``https://api.unify.ai/v0/admin/billing/invoice-month``
    with a static admin Bearer token in the ``Authorization`` header.
  * Cloud Scheduler is preferred over GHA cron for prod billing
    because it gives stronger on-time delivery guarantees, automatic
    retries with exponential backoff (``maxBackoff=3600s``,
    ``maxDoublings=5``, ``attemptDeadline=180s``), and a managed-SLA
    that GHA cron explicitly does NOT promise (GHA cron is best-
    effort and can drift 15-30 min under platform load).

Staging has no scheduled trigger — invoke on demand via
``POST {staging-base}/v0/admin/billing/invoice-month`` (or via the
``trigger_monthly_invoicing`` admin endpoint) when verifying changes
before they hit production a month later.

To re-provision the production scheduler job from scratch (e.g. after
a project recreation), use:

    gcloud scheduler jobs create http orchestra-production-monthly-invoicer \\
        --project=saas-368716 \\
        --location=us-central1 \\
        --schedule='0 2 1 * *' \\
        --time-zone=Etc/UTC \\
        --uri=https://api.unify.ai/v0/admin/billing/invoice-month \\
        --http-method=POST \\
        --headers='Authorization=Bearer $ORCHESTRA_ADMIN_KEY' \\
        --attempt-deadline=180s \\
        --min-backoff=5s --max-backoff=3600s --max-doublings=5
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    BillingAccount,
    Recharge,
    RechargeStatus,
)
from orchestra.lib.time import month_end_utc
from orchestra.web.api.utils.business_validation import get_stripe_tax_id_type
from orchestra.web.api.utils.prometheus_middleware import INVOICE_CREATED_TOTAL
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


@dataclass
class InvoiceResult:
    """Summary returned by ``invoice_month``."""

    period: str = ""
    accounts_invoiced: int = 0
    accounts_skipped: int = 0
    accounts_failed: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# public API                                                                  #
# --------------------------------------------------------------------------- #
def invoice_month(
    year: int | None = None,
    month: int | None = None,
    session: Session | None = None,
) -> InvoiceResult:
    """
    Invoice the given period; defaults to the *previous* month if omitted.
    """
    today = _dt.datetime.now(_dt.timezone.utc).date()

    if year is None or month is None:
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - _dt.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    group_day = month_end_utc(_dt.date(year, month, 1))

    if session is not None:
        return _invoice_month_with_session(session, group_day, year, month)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        return _invoice_month_with_session(session, group_day, year, month)


def _invoice_month_with_session(
    session: Session,
    group_day: _dt.date,
    year: int,
    month: int,
) -> InvoiceResult:
    """
    Internal function to handle invoicing within a given session.

    Groups all recharges by billing_account_id and creates one invoice per
    billing account.  Each account is wrapped in a savepoint — if one
    account's Stripe call fails, the others are unaffected.
    """
    result = InvoiceResult(period=f"{year}-{month:02d}")

    rows: List[Recharge] = (
        session.execute(
            select(Recharge)
            .where(
                Recharge.status == RechargeStatus.PENDING_INVOICE,
                Recharge.invoice_group == group_day,
            )
            .with_for_update(skip_locked=True),
        )
        .scalars()
        .all()
    )

    if not rows:
        return result

    # Filter rule: keep rows that belong to the CREDITS world.
    #
    # We key off the recharge's *own* plan attribution
    # (``Recharge.plan_id`` → ``BillingPlanAssignment.template.billing_mode``)
    # rather than the account's *live* billing mode. The two diverge
    # any time an account changes plans mid-month (CREDITS → METERED
    # or vice versa), and the live mode is the wrong basis: a CREDITS
    # auto-recharge that fired before the switch is still a CREDITS
    # liability that has to invoice, even after the account goes
    # METERED.
    #
    # By invariant (see ``Recharge.plan_id`` docstring):
    #   - ``plan_id IS NULL``                → CREDITS row (auto-recharge,
    #                                          payment, promo) — invoice it.
    #   - ``plan_id`` → CREDITS template      → CREDITS row — invoice it.
    #   - ``plan_id`` → METERED template      → METERED row — skip; the
    #                                          metered invoicer owns it.
    #
    # ``set_plan`` also refuses outright when PENDING_INVOICE rows are
    # in flight (see ``PendingRechargesError``), so the
    # post-switch-stranded-row case is doubly defended; this filter
    # remains the second line of defence for data-corruption
    # scenarios that bypass the DAO.
    from orchestra.db.models.orchestra_models import (
        BillingMode,
        BillingPlanAssignment,
        BillingPlanTemplate,
    )

    plan_ids = {r.plan_id for r in rows if r.plan_id is not None}
    plan_modes: Dict[int, str] = {}
    if plan_ids:
        plan_modes = dict(
            session.execute(
                select(BillingPlanAssignment.id, BillingPlanTemplate.billing_mode)
                .join(
                    BillingPlanTemplate,
                    BillingPlanTemplate.id == BillingPlanAssignment.template_id,
                )
                .where(BillingPlanAssignment.id.in_(plan_ids)),
            ).all(),
        )

    filtered: List[Recharge] = []
    for row in rows:
        if row.plan_id is None:
            filtered.append(row)
            continue
        plan_mode = plan_modes.get(row.plan_id)
        if plan_mode == BillingMode.METERED:
            logger.info(
                "Skipping PENDING_INVOICE recharge %s — plan_id=%s resolves to "
                "a METERED template; row belongs to monthly_metered_invoicer.",
                row.id,
                row.plan_id,
            )
            continue
        filtered.append(row)
    rows = filtered

    if not rows:
        return result

    from orchestra.lib.billing import configure_stripe

    configure_stripe()

    buckets: Dict[int, List[Recharge]] = {}
    for r in rows:
        buckets.setdefault(r.billing_account_id, []).append(r)

    for ba_id, bucket in buckets.items():
        ba: BillingAccount = bucket[0].billing_account

        if not ba or not ba.stripe_customer_id:
            logger.warning(
                "BillingAccount %s has recharges but no stripe_customer_id — "
                "skipping %d recharge(s) for %s",
                ba_id,
                len(bucket),
                result.period,
            )
            result.accounts_skipped += 1
            continue

        total_usd: Decimal = sum(r.amount_usd for r in bucket)
        total_cr: Decimal = sum(r.quantity for r in bucket)

        if total_cr != total_usd:
            msg = (
                f"Credit/USD ratio mismatch for billing_account {ba_id}: "
                f"{total_cr} credits != ${total_usd}. Expected 1:1 ratio."
            )
            logger.error(msg)
            result.accounts_failed += 1
            result.errors.append(msg)
            continue

        quantity = int(total_cr)
        if quantity == 0:
            result.accounts_skipped += 1
            continue

        # The Stripe call is the only step that can fail.  If it
        # succeeds we update the in-memory recharge rows; if it fails
        # we skip this account and continue with the next.  The final
        # session.commit() persists all successful updates.  If that
        # commit itself fails (DB down), the webhook self-healing in
        # _resolve_recharges_for_invoice will link orphaned recharges
        # when the invoice.payment_succeeded webhook arrives.
        try:
            idem_base = f"ba-{ba_id}-{group_day}"

            customer_tax_ids = []
            if ba.tax_id:
                tax_id_type = ba.tax_id_type
                if not tax_id_type:
                    country = None
                    if ba.billing_address and isinstance(ba.billing_address, dict):
                        country = ba.billing_address.get("country")
                    tax_id_type = get_stripe_tax_id_type(country)

                customer_tax_ids = [
                    {
                        "type": tax_id_type,
                        "value": ba.tax_id,
                    },
                ]

            invoice_params = {
                "customer": ba.stripe_customer_id,
                "automatic_tax": {"enabled": True},
                "auto_advance": True,
                "pending_invoice_items_behavior": "include",
                "description": f"Monthly invoice for {year}-{month:02d}",
                "payment_settings": {
                    "payment_method_options": {
                        "card": {"request_three_d_secure": "any"},
                    },
                },
                "metadata": {
                    "invoice_group": str(group_day),
                    "billing_account_id": str(ba_id),
                    "period": f"{year}-{month:02d}",
                },
            }

            if customer_tax_ids:
                invoice_params["customer_tax_ids"] = customer_tax_ids

            invoice = stripe.Invoice.create(
                **invoice_params,
                idempotency_key=idem_base,
            )

        except Exception as e:
            msg = (
                f"Failed to invoice billing_account {ba_id} for "
                f"{result.period}: {type(e).__name__}: {e}"
            )
            logger.error(msg)
            result.accounts_failed += 1
            result.errors.append(msg)
            continue

        for r in bucket:
            r.status = RechargeStatus.INVOICE_CREATED
            r.stripe_invoice_id = invoice.id

        INVOICE_CREATED_TOTAL.labels(
            entity_type="billing_account",
            entity_id=str(ba_id),
        ).inc()

        result.accounts_invoiced += 1

        logger.info(
            "Invoice created for billing_account %s: $%s (%s credits). "
            "Invoice ID: %s",
            ba_id,
            total_usd,
            quantity,
            invoice.id,
        )

    session.commit()
    return result
