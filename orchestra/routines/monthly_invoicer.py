"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job is meant to run once a month (e.g. 00:05 on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total
3. updates all rows to INVOICE_CREATED and stores the invoice-id
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Dict, List

import stripe
from sqlalchemy import select

from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
from orchestra.db.session import SessionLocal
from orchestra.lib.time import month_end_utc  # helper already exists
from orchestra.observability.metrics import invoice_created_total


# --------------------------------------------------------------------------- #
# public API                                                                  #
# --------------------------------------------------------------------------- #
def invoice_month(  # Celery entry-point
    year: int | None = None,
    month: int | None = None,
) -> None:
    """
    Invoice the given period; defaults to the *previous* month if omitted.
    """
    # Use UTC so "previous month" is calculated consistently on any host
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if year is None or month is None:
        # default → last month (so job on 1st invoices the month we just left)
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - _dt.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    group_day = month_end_utc(_dt.date(year, month, 1))

    with SessionLocal() as session:
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
            cents = int(total_usd.quantize(Decimal("0.01")) * 100)
            if cents == 0:  # nothing to bill
                continue

            try:
                idem_base = f"{user_id}-{group_day}"

                # 1. create invoice-item (pending)
                stripe.InvoiceItem.create(
                    customer=user.stripe_customer_id,
                    amount=cents,
                    currency="usd",
                    description=f"{total_cr} credits",
                    idempotency_key=idem_base + "-item",
                )

                # 2. create invoice which pulls the pending items
                invoice = stripe.Invoice.create(
                    customer=user.stripe_customer_id,
                    auto_advance=True,
                    description=f"{total_cr} credits used in {year}-{month:02d}",
                    metadata={"invoice_group": str(group_day)},
                    idempotency_key=idem_base,
                )

            except Exception:  # e.g. network / Stripe error
                session.rollback()
                raise

            # mark rows only AFTER Stripe succeeded
            for r in bucket:
                r.status = RechargeStatus.INVOICE_CREATED
                r.stripe_invoice_id = invoice.id

            invoice_created_total.inc()  # ← metric

        session.commit()

    def _queue_recharge(self, user_id: str, group: date) -> bool:
        """Queue recharges for invoicing - only PENDING_INVOICE rows."""
        pending_recharges = (
            self.session.query(Recharge)
            .filter_by(
                user_id=user_id,
                status=RechargeStatus.PENDING_INVOICE,  # ← Excludes PAID rows
                invoice_group=group,
            )
            .all()
        )
        # ... rest unchanged
