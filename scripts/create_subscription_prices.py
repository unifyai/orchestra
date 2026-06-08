#!/usr/bin/env python3
"""Create the recurring Stripe Prices that back the self-serve subscription plans.

Why this exists
---------------
The subscription billing model (forced monthly plans, credits reset each
cycle) bills through a Stripe *Subscription*, which requires a **recurring**
Price. The existing "Unify Credits" prices are ``type=one_time`` and Stripe
prices are immutable, so we cannot reuse them — we mint new recurring prices.

We deliberately **reuse the existing Products** (``Unify Credits``,
personal + business) so their tax codes carry over; we only add a new price
under each. Per the rollout plan this script is **purely additive** — it
NEVER archives or deactivates the existing one-time prices/products, so
production keeps working while staging branches are in flight.

Model
-----
At ``1 credit = $1`` the monthly price equals the credit grant, so a single
per-unit recurring price ($1.00/credit/month) covers the whole tier ladder:
the tier is expressed as the subscription ``quantity`` (e.g. quantity 50 =>
$50/mo => 50 credits/mo).

Annual variants are minted alongside at $12.00/credit/**year** (12× the
monthly rate), so the same quantity expresses the same tier rung billed
yearly (quantity 50 => $600/yr list). The annual *discount* is a separate
Stripe **coupon** applied at subscribe time (price-only), so it can be
tuned without re-minting prices and the credit grant stays a clean 12×.

Four prices total — {personal, business} × {monthly, annual} — plus one
annual coupon. Currency is USD only (GBP is display-only in the console).

Usage
-----
    # Test mode (default): reads STRIPE_SECRET_KEY_TEST or STRIPE_SECRET_KEY
    python scripts/create_subscription_prices.py

    # Live mode (explicit opt-in)
    python scripts/create_subscription_prices.py --live

    # Provide product ids / key explicitly
    python scripts/create_subscription_prices.py \
        --personal-product prod_xxx --business-product prod_yyy \
        --api-key sk_test_xxx

The script is idempotent on a best-effort basis: it looks for an existing
active recurring price under each product tagged with the same
``billing_role`` metadata and reuses it instead of creating a duplicate.
It prints the resulting price ids so they can be stored as the
``STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_PERSONAL`` / ``_BUSINESS`` secrets.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

import stripe

# 1 credit = $1 => one unit = 100 cents. Tier is the subscription quantity.
UNIT_AMOUNT_CENTS = 100
# Annual list rate: 12× the monthly per-credit rate (discount via coupon).
ANNUAL_MONTHS = 12
ANNUAL_UNIT_AMOUNT_CENTS = UNIT_AMOUNT_CENTS * ANNUAL_MONTHS
CURRENCY = "usd"
INTERVAL = "month"
ANNUAL_INTERVAL = "year"
# Prices are tax-exclusive (Stripe Tax adds VAT/sales tax on top of the
# per-credit rate). Required for ``automatic_tax`` — Stripe rejects tax
# calculation on prices whose tax_behavior is left ``unspecified``.
TAX_BEHAVIOR = "exclusive"

PERSONAL_ROLE = "subscription_credits_personal"
BUSINESS_ROLE = "subscription_credits_business"
PERSONAL_ROLE_ANNUAL = "subscription_credits_personal_annual"
BUSINESS_ROLE_ANNUAL = "subscription_credits_business_annual"

# Coupon identity (idempotent on this metadata tag).
ANNUAL_COUPON_ROLE = "subscription_annual_discount"
DEFAULT_ANNUAL_DISCOUNT_PERCENT = 20


def _resolve_api_key(args: argparse.Namespace) -> str:
    if args.api_key:
        return args.api_key
    if args.live:
        key = os.environ.get("STRIPE_SECRET_KEY_LIVE") or os.environ.get("STRIPE_SECRET_KEY")
    else:
        key = os.environ.get("STRIPE_SECRET_KEY_TEST") or os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        sys.exit(
            "No Stripe API key. Pass --api-key or set STRIPE_SECRET_KEY"
            f"{'_LIVE' if args.live else '_TEST'}.",
        )
    if args.live and not key.startswith("sk_live"):
        sys.exit("--live was passed but the resolved key is not an sk_live key. Aborting.")
    if not args.live and key.startswith("sk_live"):
        sys.exit("Refusing to run against a live key without --live. Aborting.")
    return key


def _find_existing_price(product_id: str, role: str) -> Optional[stripe.Price]:
    """Return an active recurring price under ``product_id`` tagged ``role``."""
    prices = stripe.Price.list(product=product_id, active=True, type="recurring", limit=100)
    for price in prices.auto_paging_iter():
        if (price.metadata or {}).get("billing_role") == role:
            return price
    return None


def _ensure_price(
    product_id: str,
    role: str,
    nickname: str,
    *,
    unit_amount: int = UNIT_AMOUNT_CENTS,
    interval: str = INTERVAL,
) -> stripe.Price:
    existing = _find_existing_price(product_id, role)
    if existing is not None:
        print(f"  reuse existing price {existing.id} (role={role})")
        return existing
    price = stripe.Price.create(
        product=product_id,
        currency=CURRENCY,
        unit_amount=unit_amount,
        recurring={"interval": interval, "usage_type": "licensed"},
        nickname=nickname,
        tax_behavior=TAX_BEHAVIOR,
        metadata={"billing_role": role},
        # idempotency: re-running with the same role won't double-create
        idempotency_key=f"sub-price-{role}-{product_id}",
    )
    print(f"  created price {price.id} (role={role})")
    return price


def _find_existing_coupon(role: str) -> Optional[stripe.Coupon]:
    """Return an existing coupon tagged with our ``billing_role`` metadata."""
    coupons = stripe.Coupon.list(limit=100)
    for coupon in coupons.auto_paging_iter():
        if (coupon.metadata or {}).get("billing_role") == role:
            return coupon
    return None


def _ensure_annual_coupon(percent_off: int) -> stripe.Coupon:
    """Create (or reuse) the forever percent-off coupon for annual plans."""
    existing = _find_existing_coupon(ANNUAL_COUPON_ROLE)
    if existing is not None:
        print(f"  reuse existing coupon {existing.id} (role={ANNUAL_COUPON_ROLE})")
        return existing
    coupon = stripe.Coupon.create(
        percent_off=percent_off,
        duration="forever",
        name=f"Annual plan discount ({percent_off}% off)",
        metadata={"billing_role": ANNUAL_COUPON_ROLE},
        idempotency_key=f"sub-coupon-{ANNUAL_COUPON_ROLE}-{percent_off}",
    )
    print(f"  created coupon {coupon.id} ({percent_off}% off, forever)")
    return coupon


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Operate against Stripe live mode.")
    parser.add_argument("--api-key", default=None, help="Stripe secret key (overrides env).")
    parser.add_argument(
        "--personal-product",
        default=os.environ.get("STRIPE_UNIFY_CREDITS_PRODUCT_ID_PERSONAL"),
        help="Product id for personal subscription prices (reuses Unify Credits product).",
    )
    parser.add_argument(
        "--business-product",
        default=os.environ.get("STRIPE_UNIFY_CREDITS_PRODUCT_ID_BUSINESS"),
        help="Product id for business subscription prices (reuses Unify Credits product).",
    )
    parser.add_argument(
        "--annual-discount-percent",
        type=int,
        default=int(
            os.environ.get(
                "STRIPE_ANNUAL_DISCOUNT_PERCENT",
                str(DEFAULT_ANNUAL_DISCOUNT_PERCENT),
            ),
        ),
        help="Percent off applied by the annual coupon (default 20).",
    )
    args = parser.parse_args()

    if not args.personal_product or not args.business_product:
        sys.exit(
            "Both --personal-product and --business-product (or the matching "
            "STRIPE_UNIFY_CREDITS_PRODUCT_ID_* env vars) are required.",
        )

    stripe.api_key = _resolve_api_key(args)
    mode = "LIVE" if args.live else "TEST"
    print(f"Creating recurring subscription prices in Stripe {mode} mode")
    print("(existing one-time prices/products are left untouched)\n")

    print(f"Personal product {args.personal_product}:")
    personal = _ensure_price(
        args.personal_product,
        PERSONAL_ROLE,
        "Unify Plan - Personal (per-credit monthly)",
    )
    personal_annual = _ensure_price(
        args.personal_product,
        PERSONAL_ROLE_ANNUAL,
        "Unify Plan - Personal (per-credit annual)",
        unit_amount=ANNUAL_UNIT_AMOUNT_CENTS,
        interval=ANNUAL_INTERVAL,
    )

    print(f"Business product {args.business_product}:")
    business = _ensure_price(
        args.business_product,
        BUSINESS_ROLE,
        "Unify Plan - Business (per-credit monthly)",
    )
    business_annual = _ensure_price(
        args.business_product,
        BUSINESS_ROLE_ANNUAL,
        "Unify Plan - Business (per-credit annual)",
        unit_amount=ANNUAL_UNIT_AMOUNT_CENTS,
        interval=ANNUAL_INTERVAL,
    )

    print("\nAnnual discount coupon:")
    coupon = _ensure_annual_coupon(args.annual_discount_percent)

    suffix = "_LIVE" if args.live else "_TEST"
    print("\nDone. Store these as secrets:")
    print(f"  STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_PERSONAL_MONTHLY{suffix}={personal.id}")
    print(f"  STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_BUSINESS_MONTHLY{suffix}={business.id}")
    print(f"  STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_PERSONAL_ANNUAL{suffix}={personal_annual.id}")
    print(f"  STRIPE_UNIFY_SUBSCRIPTION_PRICE_ID_BUSINESS_ANNUAL{suffix}={business_annual.id}")
    print(f"  STRIPE_UNIFY_ANNUAL_COUPON_ID{suffix}={coupon.id}")


if __name__ == "__main__":
    main()
