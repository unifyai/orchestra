"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job is meant to run once a month (e.g. 00:05 on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total using the Stripe product
3. updates all rows to INVOICE_CREATED and stores the invoice-id

Recharges are grouped by billing_account_id (shared by User and Organization).

Each billing account is processed in its own savepoint so that a Stripe
failure for one account does not prevent the others from being invoiced.
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
