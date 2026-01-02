"""Aggregate all "PENDING_INVOICE" recharges into a single Stripe invoice.

The job is meant to run once a month (e.g. 00:05 on the 1st) and:

1. picks every `Recharge` row whose
      • status        == PENDING_INVOICE
      • invoice_group == last day of the target month (UTC)
2. creates a single Stripe invoice + invoice-item for the total using the Stripe product
3. updates all rows to INVOICE_CREATED and stores the invoice-id

Supports both user recharges (user_id set) and organization recharges (organization_id set).
"""

from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal
from typing import Dict, List, Optional

import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import Organization, Recharge, RechargeStatus
from orchestra.lib.time import month_end_utc  # helper already exists
from orchestra.web.api.utils.prometheus_middleware import INVOICE_CREATED_TOTAL
from orchestra.web.lifetime import get_engine


def _get_tax_id_type_for_country(country_code: Optional[str]) -> str:
    """
    Determine the Stripe tax ID type based on country code.

    :param country_code: ISO 3166-1 alpha-2 country code (e.g., "US", "GB", "IN").
    :return: Stripe tax ID type string.
    """
    if not country_code:
        return "eu_vat"  # Default

    country_code = country_code.upper()

    tax_type_map = {
        "GB": "gb_vat",
        "AU": "au_abn",
        "US": "us_ein",
        "CA": "ca_gst_hst",
        "IN": "in_gst",
        "NZ": "nz_gst",
        "SG": "sg_gst",
        "CH": "ch_vat",
        "NO": "no_vat",
        "JP": "jp_cn",
        "KR": "kr_brn",
        "MX": "mx_rfc",
        "BR": "br_cnpj",
        "ZA": "za_vat",
    }

    # EU countries use eu_vat
    eu_countries = {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }

    if country_code in eu_countries:
        return "eu_vat"

    return tax_type_map.get(country_code, "eu_vat")


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
    """
    Internal function to handle invoicing within a given session.

    Processes both user recharges and organization recharges separately.
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

    # ── Separate user recharges from organization recharges ──
    user_rows = [r for r in rows if r.user_id is not None]
    org_rows = [r for r in rows if r.organization_id is not None]

    # ── Process USER recharges ──
    _invoice_user_recharges(session, user_rows, group_day, year, month)

    # ── Process ORGANIZATION recharges ──
    _invoice_org_recharges(session, org_rows, group_day, year, month)

    session.commit()


def _invoice_user_recharges(
    session: Session,
    rows: List[Recharge],
    group_day: _dt.date,
    year: int,
    month: int,
) -> None:
    """Process user recharges and create invoices."""
    if not rows:
        return

    # Group by user_id
    buckets: Dict[str, List[Recharge]] = {}
    for r in rows:
        buckets.setdefault(r.user_id, []).append(r)

    for user_id, bucket in buckets.items():
        user = bucket[0].user

        # ─────────────── pre-flight guards ───────────────
        if not user or not user.stripe_customer_id:  # legacy / free account
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
            idem_base = f"user-{user_id}-{group_day}"

            # Get the auth user for business tax information
            from orchestra.db.dao.auth_user_dao import AuthUserDAO

            auth_user_dao = AuthUserDAO(session)
            auth_user_row = auth_user_dao.get_by_id(user_id)
            auth_user = auth_user_row[0] if auth_user_row else None

            # Prepare customer tax IDs for business accounts
            customer_tax_ids = []
            if auth_user and auth_user.account_type == "business" and auth_user.tax_id:
                tax_id_type = _get_tax_id_type_for_country(auth_user.business_country)
                customer_tax_ids = [
                    {
                        "type": tax_id_type,
                        "value": auth_user.tax_id,
                    },
                ]

            invoice_params = {
                "customer": user.stripe_customer_id,
                "automatic_tax": {"enabled": True},
                "auto_advance": True,
                "pending_invoice_items_behavior": "include",
                "description": f"Monthly invoice for {year}-{month:02d}",
                "metadata": {
                    "invoice_group": str(group_day),
                    "user_id": user_id,
                    "period": f"{year}-{month:02d}",
                },
                "idempotency_key": idem_base,
            }

            if customer_tax_ids:
                invoice_params["customer_tax_ids"] = customer_tax_ids

            invoice = stripe.Invoice.create(**invoice_params)

        except stripe.error.StripeError as e:
            print(
                f"ERROR: Stripe error for user {user_id}: {str(e)}. "
                f"Error code: {getattr(e, 'code', 'unknown')}, "
                f"Type: {getattr(e, 'type', 'unknown')}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except ValueError as e:
            print(
                f"ERROR: Validation error for user {user_id}: {str(e)}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except Exception as e:
            print(
                f"ERROR: Unexpected error for user {user_id}: {str(e)}. "
                f"Period: {year}-{month:02d}. Error type: {type(e).__name__}",
            )
            session.rollback()
            raise

        # mark rows only AFTER Stripe succeeded
        for r in bucket:
            r.status = RechargeStatus.INVOICE_CREATED
            r.stripe_invoice_id = invoice.id

        INVOICE_CREATED_TOTAL.labels(entity_type="user", entity_id=user_id).inc()


def _invoice_org_recharges(
    session: Session,
    rows: List[Recharge],
    group_day: _dt.date,
    year: int,
    month: int,
) -> None:
    """
    Process organization recharges and create invoices.

    Organizations with direct billing (stripe_customer_id set) receive
    their own invoices. All member usage under org context is aggregated
    into a single monthly org invoice.
    """
    if not rows:
        return

    # Group by organization_id
    buckets: Dict[int, List[Recharge]] = {}
    for r in rows:
        buckets.setdefault(r.organization_id, []).append(r)

    for org_id, bucket in buckets.items():
        org: Organization = bucket[0].organization

        # ─────────────── pre-flight guards ───────────────
        if not org or not org.stripe_customer_id:
            # Org without direct billing - skip (shouldn't happen, but safety check)
            print(
                f"WARNING: Org {org_id} has recharges but no stripe_customer_id. "
                f"Skipping {len(bucket)} recharge(s).",
            )
            continue

        total_usd: Decimal = sum(r.amount_usd for r in bucket)
        total_cr: Decimal = sum(r.quantity for r in bucket)

        # With 1 credit = $1, total_cr should equal total_usd
        if total_cr != total_usd:
            raise ValueError(
                f"Credit/USD ratio mismatch for org {org_id}: "
                f"{total_cr} credits != ${total_usd}. Expected 1:1 ratio.",
            )

        quantity = int(total_cr)

        if quantity == 0:
            continue

        try:
            idem_base = f"org-{org_id}-{group_day}"

            # Prepare customer tax IDs from org's business profile
            customer_tax_ids = []
            if org.tax_id:
                # Get country from billing_address (JSONB)
                country = None
                if org.billing_address and isinstance(org.billing_address, dict):
                    country = org.billing_address.get("country")

                tax_id_type = _get_tax_id_type_for_country(country)
                customer_tax_ids = [
                    {
                        "type": tax_id_type,
                        "value": org.tax_id,
                    },
                ]

            invoice_params = {
                "customer": org.stripe_customer_id,
                "automatic_tax": {"enabled": True},
                "auto_advance": True,
                "pending_invoice_items_behavior": "include",
                "description": f"Monthly invoice for {year}-{month:02d}",
                "metadata": {
                    "invoice_group": str(group_day),
                    "organization_id": str(org_id),
                    "organization_name": org.name,
                    "period": f"{year}-{month:02d}",
                },
                "idempotency_key": idem_base,
            }

            if customer_tax_ids:
                invoice_params["customer_tax_ids"] = customer_tax_ids

            invoice = stripe.Invoice.create(**invoice_params)

            print(
                f"SUCCESS: Created invoice for org {org_id} ({org.name}): "
                f"${total_usd} ({quantity} credits). Invoice ID: {invoice.id}",
            )

        except stripe.error.StripeError as e:
            print(
                f"ERROR: Stripe error for org {org_id}: {str(e)}. "
                f"Error code: {getattr(e, 'code', 'unknown')}, "
                f"Type: {getattr(e, 'type', 'unknown')}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except ValueError as e:
            print(
                f"ERROR: Validation error for org {org_id}: {str(e)}. "
                f"Period: {year}-{month:02d}",
            )
            session.rollback()
            raise
        except Exception as e:
            print(
                f"ERROR: Unexpected error for org {org_id}: {str(e)}. "
                f"Period: {year}-{month:02d}. Error type: {type(e).__name__}",
            )
            session.rollback()
            raise

        # mark rows only AFTER Stripe succeeded
        for r in bucket:
            r.status = RechargeStatus.INVOICE_CREATED
            r.stripe_invoice_id = invoice.id

        INVOICE_CREATED_TOTAL.labels(
            entity_type="organization",
            entity_id=str(org_id),
        ).inc()
