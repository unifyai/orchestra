"""Enums and sentinel constants.

Lives outside ``orchestra_models.py`` so that ``lib/``, ``routines/``,
``web/api/``, and even external SDKs can import these without dragging
in the full 4k-line ORM module. The values here are also the literal
strings written to Postgres columns (``StrEnum``) and to JSON API
payloads, so they are part of the persisted public contract — change
existing values only via a migration + API-version bump.
"""

from enum import Enum

# Python 3.11 ships enum.StrEnum. – Provide a fallback for older versions.
try:
    from enum import StrEnum
except ImportError:  # pragma: no cover

    class StrEnum(str, Enum):  # type: ignore[override]
        """Minimal back-port of enum.StrEnum."""


# ---------------------------------------------------------------------------
# Recharge ledger
# ---------------------------------------------------------------------------


class RechargeStatus(StrEnum):
    """Lifecycle of a row in the ``recharge`` table.

    Mirrors the DB type ``recharge_status`` (see migration 20250520…).
    """

    PENDING_INVOICE = "PENDING_INVOICE"
    INVOICE_CREATED = "INVOICE_CREATED"
    PAID = "PAID"
    FAILED = "FAILED"
    DISPUTED = "DISPUTED"


# Recharge type constants. Plain strings (not enum members) for backward
# compatibility with existing rows and call sites that pass these as
# ``type="auto"`` literals.
RECHARGE_TYPE_AUTO = "auto"
RECHARGE_TYPE_PAYMENT = "payment"
RECHARGE_TYPE_PROMO = "promo"
RECHARGE_TYPE_MONTHLY_COMMIT = "monthly_commit"
RECHARGE_TYPE_OVERAGE_TRUEUP = "overage_trueup"


# ---------------------------------------------------------------------------
# Billing Plan shape
# ---------------------------------------------------------------------------
#
# Notes on what's *not* an enum here:
#
# * "Plan type" (PAY_AS_YOU_GO vs COMMITMENT) is no longer a stored
#   column — it's derived from ``commit_amount`` (NULL/zero ⇒ PAYG,
#   positive ⇒ COMMITMENT). One column, one invariant, no enum to keep
#   in sync.
# * Overage handling is no longer configurable: usage above commit
#   always flows through to the invoice at list price. There's no
#   ``OveragePolicy`` enum and no ``monthly_usage_cap`` — the platform
#   does not block usage based on plan terms (operators can void in
#   Stripe if a specific invoice should not stand).
# * "Catalog availability" is split into two booleans on the template
#   row (``is_custom`` + ``is_active``); see :class:`BillingPlanTemplate`.
#   No enum needed.


class BillingMode(StrEnum):
    CREDITS = "CREDITS"
    METERED = "METERED"


class CommitSchedule(StrEnum):
    """When the commit gets invoiced.

    ``AMORTISED`` spreads the commit evenly across the constituent months.
    ``UPFRONT`` invoices the full commit on day one of the period; subsequent
    months invoice overage only.
    """

    AMORTISED = "AMORTISED"
    UPFRONT = "UPFRONT"


class CommitPeriod(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


class CollectionMethod(StrEnum):
    """How money flows from the customer.

    ``AUTO_CARD`` mirrors today's behaviour (Stripe charges the card on file
    when the invoice finalises). ``SEND_INVOICE_NET_30`` flips Stripe to
    ``send_invoice`` mode with a 30-day due date.
    """

    AUTO_CARD = "AUTO_CARD"
    SEND_INVOICE_NET_30 = "SEND_INVOICE_NET_30"


class ProrationPolicy(StrEnum):
    """How partial first/last periods are billed."""

    PRORATE = "PRORATE"
    SKIP_FIRST = "SKIP_FIRST"
    FULL_FIRST = "FULL_FIRST"


class CreditsRolloverPolicy(StrEnum):
    """COMMITMENT+CREDITS only — what happens to unused allowance.

    Renamed from ``RolloverPolicy`` to ``CreditsRolloverPolicy`` so the
    name itself signals the (COMMITMENT, CREDITS) cell it applies to —
    the column is silently NULL for any other quadrant and a check
    constraint enforces it.

    * ``ROLL_OVER``            — unused credits at month-end carry into
                                 next month's wallet (use it or save it).
    * ``FORFEIT_AT_PERIOD_END`` — unused credits expire at month-end;
                                  wallet resets to the fresh commit
                                  amount on day one (use it or lose it).
    """

    ROLL_OVER = "ROLL_OVER"
    FORFEIT_AT_PERIOD_END = "FORFEIT_AT_PERIOD_END"


class FxPolicy(StrEnum):
    """How USD-denominated ledger usage is converted into the contract currency.

    Lives on ``BillingPlanTemplate`` so each contract picks its own FX behaviour,
    mirroring the way enterprise contracts actually negotiate FX clauses:

    * ``LOCKED_RATE``    — a single rate, agreed at contract signing, applied
                           for the lifetime of the template. Stored on the
                           template itself in ``fx_locked_rate``. Removes FX
                           risk for both sides — the typical 12-month enterprise
                           ask. No external dependency at invoice time.
    * ``SPOT``           — the rate published by Frankfurter (ECB-sourced)
                           for the period-end date. Live HTTP call at
                           invoice time; the resolved rate is pinned in
                           ``Recharge.detail`` so re-runs are deterministic
                           without needing a snapshot table.
    * ``PERIOD_AVERAGE`` — average of business-day rates from Frankfurter
                           across the entire billing period. Smooths intra-month
                           volatility; same pinning behaviour as ``SPOT``.

    USD-denominated templates have ``fx_policy`` NULL (no conversion needed)
    rather than a "no-op" policy value; a check constraint enforces that
    ``fx_policy`` is NULL iff ``currency = 'USD'``.

    Adding a new policy means: extend this enum, add a branch in
    ``orchestra/routines/monthly_metered_invoicer._resolve_fx_rate``, update
    the ``ck_plan_template_fx_policy`` check constraint, and ship a migration.
    """

    LOCKED_RATE = "LOCKED_RATE"
    SPOT = "SPOT"
    PERIOD_AVERAGE = "PERIOD_AVERAGE"


class PaymentMethodType(StrEnum):
    """Stripe payment method types we support on hosted invoices.

    Stored as the literal Stripe API value (lower-case, snake_case) in
    ``BillingAccount.preferred_payment_method_types`` so we can pass the
    column straight into ``Invoice.payment_settings.payment_method_types``
    without translation.

    The set is intentionally small. Add a new member only after the
    matching method is enabled in the Stripe Dashboard *and* the
    invoicer knows how to populate any required
    ``payment_method_options`` (see
    ``monthly_metered_invoicer._payment_method_options``).

    * ``CARD`` — credit/debit card. Always supported. Default for AUTO_CARD.
    * ``CUSTOMER_BALANCE`` — wire transfer via Stripe-issued virtual bank
      account. Customer pushes funds to Stripe; Stripe credits the
      ``Customer.cash_balance`` and auto-applies it to outstanding
      invoices. SEND_INVOICE_NET_30 only — there's nothing to "auto-pull".
      Supported currencies: USD, GBP, EUR (BE/DE/ES/FR/IE/NL only),
      JPY, MXN — see ``_BANK_TRANSFER_TYPE_BY_CURRENCY`` in
      ``monthly_metered_invoicer``. Other currencies fall back to
      card-only at invoice time.
    """

    CARD = "card"
    CUSTOMER_BALANCE = "customer_balance"


# ---------------------------------------------------------------------------
# Billing Sentinel: the implicit platform-default plan
# ---------------------------------------------------------------------------
#
# Seeded by the ``metered_and_billing_plan`` migration; NEVER renumber.
#
# Every account always has an active ``BillingPlanAssignment`` pointing
# at a template — pristine self-serve accounts get a row whose
# ``template_id == DEFAULT_TEMPLATE_ID`` at signup
# (``BillingAccountDAO.create``), and the migration backfilled one for
# every pre-v2 account. Cancelling a non-default plan inserts a fresh
# default-template row; there is no "no row / NULL pointer" shape in
# steady state. ``BillingAccount.plan_assignment_id`` is nullable in the
# DB only because ``NOT NULL`` is not deferrable
# (chicken-and-egg at row-creation time); a NULL in production is
# corruption that the daily reconciliation routine flags as critical.
#
# ``BillingAccountDAO.resolve_billing_mode()`` and
# ``BillingPlanAssignmentDAO.resolve_effective_plan()`` both rely on
# this invariant.
DEFAULT_TEMPLATE_ID = 1


# ---------------------------------------------------------------------------
# Billing Sentinel: the implicit platform-default plan group
# ---------------------------------------------------------------------------
#
# Mirrors ``DEFAULT_TEMPLATE_ID``: row id=1 is reserved for the
# system-default ``plan_group`` (seeded by the
# ``metered_and_billing_plan`` migration). Every ``BillingAccount`` is
# auto-assigned to this group at creation time so the customer-facing
# self-serve switch endpoint always has *something* to evaluate
# against — today the default group only contains
# ``DEFAULT_TEMPLATE_ID``, so the FE hide-rule (no non-current target)
# silently suppresses the "Switch plan" section. The day a second
# template (e.g. "pro") joins the default group, every account
# inherits the new switcher UX without per-account migration.
#
# ``BillingAccount.plan_group_id`` is ``NOT NULL`` (default
# ``DEFAULT_PLAN_GROUP_ID``) — semantics:
#   * 1 (sentinel)        → on the system default; auto-applied at signup.
#   * <other group id>    → operator pinned a custom group (replaces default).
#
# Customers whose current template isn't a member of their assigned
# group simply see no switcher (the customer-facing endpoint hides
# the section when no member is ``is_current``); there is no
# SQL-NULL "opt-out" state, and ``PlanGroupInUseError`` requires
# operators to *reassign* to another group (typically id=1) before
# deprecating one — not to NULL the column.
#
# NEVER renumber the row. Same convention as DEFAULT_TEMPLATE_ID.
DEFAULT_PLAN_GROUP_ID = 1
