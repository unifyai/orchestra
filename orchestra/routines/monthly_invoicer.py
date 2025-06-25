"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job is meant to run once a month (e.g. 00:05 on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total using the Stripe product
3. updates all rows to INVOICE_CREATED and stores the invoice-id
"""

from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal
from typing import Dict, List

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.lib.time import month_end_utc  # helper already exists
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
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_key:
        raise ValueError("STRIPE_SECRET_KEY environment variable not set")

    stripe.api_key = stripe_key

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
    """Internal function to handle invoicing within a given session."""
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

    # ── group rows by user so each customer receives its own invoice ──
    buckets: Dict[str, List[Recharge]] = {}
    for r in rows:
        buckets.setdefault(r.user_id, []).append(r)

    for user_id, bucket in buckets.items():
        user = bucket[0].user

        # ─────────────── pre-flight guards ───────────────
        if not user.stripe_customer_id:  # legacy / free account
            continue

        total_usd: Decimal = sum(r.amount_usd for r in bucket)
        total_cr: Decimal = sum(r.quantity for r in bucket)

        # With 1 credit = $1, total_cr should equal total_usd
        if total_cr != total_usd:
            raise ValueError(
                f"Credit/USD ratio mismatch for user {user_id}: "
                f"{total_cr} credits != ${total_usd}. Expected 1:1 ratio.",
            )

        # Use credits as the quantity (1 credit = $1 with Stripe product)
        quantity = int(total_cr)

        if quantity == 0:  # nothing to bill
            continue

        try:
            idem_base = f"{user_id}-{group_day}"

            # 1. create invoice-item using amount instead of price to avoid custom_unit_amount issues
            stripe.InvoiceItem.create(
                customer=user.stripe_customer_id,
                amount=int(total_usd * 100),  # Convert to cents
                currency="usd",
                description=f"{total_cr} credits used in {year}-{month:02d}",
                metadata={
                    "invoice_group": str(group_day),
                    "user_id": user_id,
                    "period": f"{year}-{month:02d}",
                },
                idempotency_key=idem_base + "-item",
            )

            # 2. create invoice which pulls the pending items
            invoice = stripe.Invoice.create(
                customer=user.stripe_customer_id,
                automatic_tax={"enabled": True},  # Enable automatic tax collection
                auto_advance=True,
                pending_invoice_items_behavior="include",
                description=f"{total_cr} credits used in {year}-{month:02d}",
                metadata={
                    "invoice_group": str(group_day),
                    "user_id": user_id,
                    "period": f"{year}-{month:02d}",
                },
                idempotency_key=idem_base,
            )

        except Exception as e:  # e.g. network / Stripe error
            session.rollback()
            raise

        # mark rows only AFTER Stripe succeeded
        for r in bucket:
            r.status = RechargeStatus.INVOICE_CREATED
            r.stripe_invoice_id = invoice.id

        INVOICE_CREATED_TOTAL.labels(user_id=user_id).inc()  # ← metric

    session.commit()
