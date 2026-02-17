"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job is meant to run once a month (e.g. 00:05 on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total using the Stripe product
3. updates all rows to INVOICE_CREATED and stores the invoice-id

Recharges are grouped by billing_account_id (shared by User and Organization).
"""

from __future__ import annotations

import datetime as _dt
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
from orchestra.lib.time import month_end_utc  # helper already exists
from orchestra.settings import settings
from orchestra.web.api.utils.business_validation import get_stripe_tax_id_type
from orchestra.web.api.utils.prometheus_middleware import INVOICE_CREATED_TOTAL
from orchestra.web.lifetime import get_engine


# --------------------------------------------------------------------------- #
# public API                                                                  #
# --------------------------------------------------------------------------- #
def invoice_month(
    year: int | None = None,
    month: int | None = None,
    session: Session | None = None,
) -> None:
    """
    Invoice the given period; defaults to the *previous* month if omitted.
    """
    # Configure Stripe API key
    if not settings.stripe_secret_key:
        raise ValueError("stripe_secret_key not configured in settings")

    stripe.api_key = settings.stripe_secret_key

    # Use UTC so "previous month" is calculated consistently on any host
    today = _dt.datetime.now(_dt.timezone.utc).date()

    if year is None or month is None:
        # default → last month (so job on 1st invoices the month we just left)
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - _dt.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    group_day = month_end_utc(_dt.date(year, month, 1))

    if session is not None:
        # Use provided session
        _invoice_month_with_session(session, group_day, year, month)
    else:
        # Create own session (for backward compatibility)
        SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
        with SessionLocal() as session:
            _invoice_month_with_session(session, group_day, year, month)


def _invoice_month_with_session(
    session: Session,
    group_day: _dt.date,
    year: int,
    month: int,
) -> None:
    """
    Internal function to handle invoicing within a given session.

    Groups all recharges by billing_account_id and creates one invoice per
    billing account.
    """
    # Lock rows so concurrent workers do not double-invoice
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
        return  # nothing to do for that month

    # Group all recharges by billing_account_id
    buckets: Dict[int, List[Recharge]] = {}
    for r in rows:
        buckets.setdefault(r.billing_account_id, []).append(r)

    for ba_id, bucket in buckets.items():
        ba: BillingAccount = bucket[0].billing_account

        # ─────────────── pre-flight guards ───────────────
        if not ba or not ba.stripe_customer_id:
            print(
                f"WARNING: BillingAccount {ba_id} has recharges but no stripe_customer_id. "
                f"Skipping {len(bucket)} recharge(s).",
            )
            continue

        total_usd: Decimal = sum(r.amount_usd for r in bucket)
        total_cr: Decimal = sum(r.quantity for r in bucket)

        # With 1 credit = $1, total_cr should equal total_usd
        if total_cr != total_usd:
            raise ValueError(
                f"Credit/USD ratio mismatch for billing_account {ba_id}: "
                f"{total_cr} credits != ${total_usd}. Expected 1:1 ratio.",
            )

        quantity = int(total_cr)

        if quantity == 0:
            continue

        try:
            idem_base = f"ba-{ba_id}-{group_day}"

            # Prepare customer tax IDs from billing account's business profile
            customer_tax_ids = []
            if ba.tax_id:
                # Determine tax ID type from stored value or billing address country
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

            print(
                f"SUCCESS: Created invoice for billing_account {ba_id}: "
                f"${total_usd} ({quantity} credits). Invoice ID: {invoice.id}",
            )

        except stripe.error.StripeError as e:
            print(
                f"ERROR: Stripe error for billing_account {ba_id}: {str(e)}. "
                f"Error code: {getattr(e, 'code', 'unknown')}, "
                f"Type: {getattr(e, 'type', 'unknown')}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except ValueError as e:
            print(
                f"ERROR: Validation error for billing_account {ba_id}: {str(e)}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except Exception as e:
            print(
                f"ERROR: Unexpected error for billing_account {ba_id}: {str(e)}. "
                f"Period: {year}-{month:02d}. Error type: {type(e).__name__}",
            )
            session.rollback()
            raise

        # mark rows only AFTER Stripe succeeded
        for r in bucket:
            r.status = RechargeStatus.INVOICE_CREATED
            r.stripe_invoice_id = invoice.id

        INVOICE_CREATED_TOTAL.labels(
            entity_type="billing_account",
            entity_id=str(ba_id),
        ).inc()

    session.commit()
