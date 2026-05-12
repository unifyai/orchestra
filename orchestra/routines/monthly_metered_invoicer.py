"""Monthly invoicer for METERED billing accounts.

Counterpart to ``monthly_credits_invoicer.invoice_month`` (which finalises the
CREDITS-mode pipeline by aggregating ``PENDING_INVOICE`` Recharge rows).
This routine produces invoices for METERED accounts whose period just
closed.

Module map (mainly for newcomers — the file is long enough that a TOC
saves a Ctrl-F roundtrip):

* Public entrypoints
    - :func:`invoice_metered_month` — bulk run; called by the scheduler.
    - :func:`invoice_metered_month_for_account` — single-account re-run
      surface used by the admin endpoint and on-call replays.
    - :func:`estimate_in_progress_invoice` — read-only mid-period
      estimate that powers the customer-facing progress bar (no
      Recharge / Stripe writes).
* Per-account orchestrator
    - :func:`_invoice_metered_with_session` — internal session-scoped
      worker shared by both entrypoints.
    - :func:`_process_one_account` — one account, one period; raises
      :class:`_SkipAccount` for documented skip reasons.
* Plan formula plumbing
    - :func:`_aggregate_period_ledger` — sums signed
      ``CreditTransaction.amount`` rows over the period.
    - :func:`_compute_invoice_line` — applies the plan formula
      (commit + overage; PAYG; grants).
    - :func:`_build_invoice_lines` — fans the single line out into
      Stripe-shaped InvoiceItem entries.
* FX
    - :func:`_resolve_fx_rate` — picks LOCKED_RATE / SPOT /
      PERIOD_AVERAGE per the template's ``fx_policy``; pins the
      resolved rate into ``Recharge.detail`` for replay.
* Stripe wiring
    - :func:`_create_stripe_invoice` — InvoiceItems + finalise.
    - :func:`_resolve_payment_method_types` /
      :func:`_payment_method_options` /
      :func:`_bank_transfer_options` — closed-set bank-transfer
      mapping (USD/GBP/EUR/JPY/MXN) with graceful card-only fallback.
* Eligibility / dedupe
    - :func:`_find_eligible_accounts` — METERED + assignment-in-force.
    - :func:`_existing_metered_recharge` — dedupe guard so a re-run
      can't double-invoice the same period.

For each eligible account, the routine:

1. Resolves the plan assignment in force at period end. Skip if absent
   (no assignment covered that period — e.g. the account was created
   after the period, or its history pre-dates the v2 backfill) or if
   the template's ``billing_mode`` is not METERED.
2. **Always invoices regardless of ``account_status``** (ACTIVE,
   SUSPENDED, CLOSED). A non-ACTIVE account that incurred real usage
   in the period (typical mid-period suspension) still owes for what
   was rendered, and the contract commit applies for the period the
   customer signed up to. The status + suspension_reason at invoice
   time are stamped into ``Recharge.detail`` and a WARNING is logged
   so on-call sees the unusual case. Operators can void in Stripe if
   a specific invoice should not stand.
3. Sums signed ``CreditTransaction.amount`` for the period:
   * positive = grants/refunds (reduce invoice)
   * negative = usage (drive invoice)
4. Applies the plan formula. ``base_pricing_factor`` applies to
   **all** usage (commit-included on COMMITMENT plans, the entire
   line on PAYG); ``overage_pricing_factor`` is an *additional*
   multiplier stacked on top of the base rate for the overage portion
   only. So a customer on a 20% base discount + 25% overage uplift
   pays ``0.80 × 1.25 = 1.0×`` (back to list price) for above-commit
   usage. ``overage_pricing_factor = 1.0`` means "no overage
   penalty" — the discount/markup baked into ``base_pricing_factor``
   continues to apply uniformly.

   Two **independent** dimensions shape the COMMITMENT case:

   * ``commit_period`` — MONTHLY / QUARTERLY / ANNUAL — sets the
     **monthly-equivalent** floor used for overage calculation.
     ``monthly_commit = commit_amount / months_in_period``. Overage is
     computed against this monthly floor regardless of period (a
     customer on an ANNUAL $12k contract sees the same $1k/mo overage
     boundary as a MONTHLY $1k contract — they don't get to bank
     unused early-month capacity for late-month overage).
   * ``commit_schedule`` — AMORTISED / UPFRONT — controls **when**
     the commit appears on an invoice; it never affects the overage
     calculation.

       base_factor    = template.base_pricing_factor    (× fx_rate folded in)
       overage_factor = template.overage_pricing_factor (additional uplift)
       monthly_commit = commit_amount / months_in_period(commit_period)

       # COMMITMENT — overage logic is identical for both schedules
       included_capacity_local = monthly_commit / base_factor
       overage_raw             = max(0, raw_usage_local - included_capacity_local)
       overage_charge_local    = overage_raw * base_factor * overage_factor

       # COMMITMENT — commit charge depends on schedule
       if commit_schedule == 'AMORTISED':
           commit_charge_local = monthly_commit
       elif commit_schedule == 'UPFRONT':
           is_anniversary = (period_start - assignment.started_at) months
                            divisible by months_in_period(commit_period)
           commit_charge_local = commit_amount if is_anniversary else 0

       contract_usage_local    = commit_charge_local + overage_charge_local

       # PAY_AS_YOU_GO — base only, no commit, no overage
       contract_usage_local    = raw_usage_local * base_factor

       invoiced_local          = contract_usage_local - grants_local

   There is no overage cap and no usage cap — usage above the
   monthly-equivalent floor always invoices at ``base × overage``,
   and the platform never refuses to bill for delivered usage.
   Operators can void specific invoices in Stripe if a particular
   charge should not stand.

5. Creates a single Stripe Invoice broken into one InvoiceItem per
   logical line — for COMMITMENT plans that crossed into overage that
   means a separate "monthly commitment" line and a "usage overage"
   line; grants applied are surfaced as a negative "Credits applied"
   line. The Invoice itself honours the template's
   ``collection_method``:
   * AUTO_CARD            → ``collection_method='charge_automatically'``
   * SEND_INVOICE_NET_30  → ``collection_method='send_invoice'`` with
                            ``days_until_due=30``
6. Inserts a ``Recharge`` row tagged with ``plan_id`` (the assignment
   in force) and an audit ``detail`` JSONB so the formula can be replayed
   on dispute.

Period boundaries are calendar months in UTC. The job is meant to run
once per month (e.g. 00:10 on the 1st, after ``invoice_month`` has had
its chance for CREDITS accounts).

Multi-currency: USD-denominated templates have ``fx_policy=NULL`` and
no conversion happens. Non-USD templates carry an ``fx_policy``
(``LOCKED_RATE`` / ``SPOT`` / ``PERIOD_AVERAGE``) that decides how USD
ledger amounts are converted into the template's ``currency`` at
invoice time. Live-fetch policies hit Frankfurter (free, ECB-sourced)
on the spot and pin the resolved rate into ``Recharge.detail`` so
re-runs are deterministic without a snapshot table.

----------------------------------------------------------------------
Scheduling
----------------------------------------------------------------------

Production runs on **Google Cloud Scheduler** firing the admin HTTP
endpoint ``POST /v0/admin/billing/invoice-metered-month``, which is a
thin FastAPI wrapper that calls :func:`invoice_metered_month`:

  * Job ``orchestra-production-monthly-metered-invoicer`` in project
    ``saas-368716`` / location ``us-central1``.
  * Schedule ``5 2 1 * *`` UTC (02:05 on the 1st — five minutes after
    the credits-mode invoicer's prod scheduler so the older,
    well-understood credits pipeline can't be starved by this newer
    one if it misbehaves).
  * Cloud Scheduler hits ``https://api.unify.ai/v0/admin/billing/invoice-metered-month``
    with a static admin Bearer token (mirroring the credits-mode
    scheduler's auth pattern). Bulk routine safety relies on layered
    idempotency: a unique key on ``(billing_account_id,
    invoice_group)`` short-circuits at the DB-row insert, and Stripe
    idempotency keys at the line- and invoice-create call sites
    prevent double-create on Stripe's side. The endpoint additionally
    soft-rejects current/future-month requests so a misconfigured
    body can't invoice an in-progress period.
  * ``attemptDeadline`` is set to ``1800s`` (30 minutes) — comfortably
    above the worst-case bulk-run latency so a slow run isn't
    misdiagnosed as failed and retried in parallel. Retry config
    mirrors the credits scheduler (``maxBackoff=3600s``,
    ``maxDoublings=5``, ``minBackoff=5s``).
  * Cloud Scheduler is preferred over GHA cron for prod billing
    because it gives stronger on-time delivery guarantees, automatic
    retries with exponential backoff, and a managed-SLA that GHA
    cron explicitly does NOT promise.

Staging has no scheduled trigger — invoke on demand by importing
``invoice_metered_month`` from a one-off Python shell on the staging
worker pod when verifying changes before they hit production a month
later. For per-account spot-checks the single-account re-run admin
endpoint ``POST {staging-base}/v0/admin/billing/invoice-metered-month/account``
remains available (it's narrow-blast-radius and supports ``force=true``
for "I voided the prior Stripe invoice, please regenerate" recovery).
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import stripe
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_MONTHLY_COMMIT,
    BillingAccount,
    BillingMode,
    BillingPlanAssignment,
    BillingPlanTemplate,
    CollectionMethod,
    CommitPeriod,
    CommitSchedule,
    CreditTransaction,
    FxPolicy,
    PaymentMethodType,
    Recharge,
    RechargeStatus,
)
from orchestra.lib.fx import (
    FxProviderError,
    fetch_period_average,
    fetch_spot,
)
from orchestra.lib.time import month_end_utc
from orchestra.web.api.utils.business_validation import get_stripe_tax_id_type
from orchestra.web.api.utils.prometheus_middleware import INVOICE_CREATED_TOTAL
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MeteredInvoiceResult:
    """Summary returned by :func:`invoice_metered_month`."""

    period: str = ""
    accounts_invoiced: int = 0
    accounts_skipped: int = 0
    accounts_failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _ResolvedFxRate:
    """Result of resolving the FX rate for one account+period.

    Encapsulates the rate plus enough provenance to reconstruct what we
    did at invoice time. Pinned verbatim onto ``Recharge.detail`` so a
    re-run reads from there instead of re-fetching from the upstream
    provider — a re-run six months later still produces the same invoice
    even if Frankfurter has a different rate that day.
    """

    rate: Decimal
    policy: str  # FxPolicy value
    provider: Optional[str]  # NULL for USD (no FX) / LOCKED_RATE
    as_of_date: Optional[_dt.date]  # for SPOT only
    period_start: Optional[_dt.date]  # for PERIOD_AVERAGE only
    period_end: Optional[_dt.date]  # for PERIOD_AVERAGE only
    sample_dates: Optional[list[_dt.date]]  # for PERIOD_AVERAGE only

    def to_audit_dict(self) -> dict:
        return {
            "fx_rate": str(self.rate),
            "fx_policy": self.policy,
            "fx_provider": self.provider,
            "fx_as_of_date": (
                self.as_of_date.isoformat() if self.as_of_date else None
            ),
            "fx_period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "fx_period_end": (
                self.period_end.isoformat() if self.period_end else None
            ),
            "fx_sample_dates": (
                [d.isoformat() for d in self.sample_dates]
                if self.sample_dates is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Commit period / schedule helpers
# ---------------------------------------------------------------------------


_MONTHS_IN_PERIOD: dict[str, int] = {
    CommitPeriod.MONTHLY.value: 1,
    CommitPeriod.QUARTERLY.value: 3,
    CommitPeriod.ANNUAL.value: 12,
}


def _months_in_period(commit_period: Optional[str]) -> int:
    """Return the integer month-count for a ``commit_period`` enum value.

    Defaults to ``1`` for unknown / NULL values so that PAYG plans
    (which don't carry a period) and back-compat callers degrade to
    monthly behaviour rather than raising.
    """
    if commit_period is None:
        return 1
    return _MONTHS_IN_PERIOD.get(commit_period, 1)


def _is_commit_billing_period(
    *,
    started_at: _dt.datetime,
    commit_period: Optional[str],
    period_start: _dt.datetime,
) -> bool:
    """Return True iff ``period_start`` is a commit-anniversary month.

    Anniversaries are anchored on the assignment's ``started_at``. The
    customer signed up in *some* calendar month; the commit re-bills on
    the same calendar month every ``months_in_period(commit_period)``
    months thereafter. We anchor on the *month* (not the day) because
    Stripe invoices land on month boundaries — a contract started on
    Feb 15 has its first invoice on Feb 1 of the *next* month and its
    next anniversary on Feb 1 the following year (for ANNUAL).

    Pure month arithmetic — no ``timedelta(days=N)`` — so DST and
    leap years can't shift the anniversary.

    For MONTHLY commits this returns ``True`` for every period
    (anniversary every month), making the AMORTISED / UPFRONT
    distinction a no-op for monthly contracts.

    Returns ``False`` if ``period_start`` is *before* the assignment
    started — defensive for invoice replays of pre-signup periods.
    """
    months_per = _months_in_period(commit_period)
    elapsed_months = (
        (period_start.year - started_at.year) * 12
        + (period_start.month - started_at.month)
    )
    if elapsed_months < 0:
        return False
    return elapsed_months % months_per == 0


@dataclass
class _LineCalculation:
    """Internal: the formula inputs/outputs for one account in one period.

    Persisted verbatim onto ``Recharge.detail`` so a customer dispute can
    reconstruct the invoice from first principles.

    Currency-naming convention: fields suffixed ``_usd`` always hold the
    raw USD value (the currency of ``CreditTransaction.amount``). Fields
    suffixed ``_local`` hold the same quantity converted into the
    template's ``currency`` using ``fx.rate``. For USD contracts
    ``_usd`` and ``_local`` columns are equal and ``fx.rate`` is ``1``.
    The ``invoiced_*`` and ``commit_amount`` figures live in the
    contract currency because Stripe needs an invoice in one currency.

    Two pricing factors travel together: ``base_pricing_factor``
    applies to *all* usage (PAYG, commit-included, and overage);
    ``overage_pricing_factor`` is an *additional* multiplier stacked
    on top of base for the overage portion only (so the effective
    above-commit rate is ``base × overage``; ``overage = 1.0`` means
    no penalty over the base discount). Both are pinned into the
    audit dict so a re-run after a template price change still
    reconstructs what we *actually* charged at the time.

    The ``contract_usage_local`` total is decomposed into three
    mutually-exclusive components so ``_build_invoice_lines`` can
    render distinct Stripe ``InvoiceItem`` rows:

    * ``payg_charge_local``   — PAYG only: ``raw_usage_local × base_factor``.
    * ``commit_charge_local`` — COMMITMENT only: ``monthly_commit_local``
      for AMORTISED schedules, ``commit_amount`` on UPFRONT
      anniversary periods, ``0`` on UPFRONT non-anniversary periods.
    * ``overage_charge_local`` — COMMITMENT only:
      ``max(0, raw_usage - monthly_commit / base_factor) × overage_factor``.

    ``monthly_commit_local`` (per-month equivalent of the period
    commit) and ``is_commit_billing_period`` (anniversary marker, only
    meaningful for UPFRONT) are surfaced separately so the
    customer-facing usage progress bar can display the per-month
    capacity even on quarterly / annual / upfront contracts.
    """

    raw_usage_usd: Decimal
    grants_usd: Decimal
    raw_usage_local: Decimal
    grants_local: Decimal
    base_pricing_factor: Decimal
    overage_pricing_factor: Decimal
    contract_usage_local: Decimal
    payg_charge_local: Decimal
    commit_charge_local: Decimal
    overage_charge_local: Decimal
    commit_amount: Optional[Decimal]
    monthly_commit_local: Decimal
    commit_schedule: Optional[str]
    is_commit_billing_period: bool
    invoiced_local: Decimal
    currency: str
    fx: _ResolvedFxRate

    def to_audit_dict(self, period_start: _dt.datetime, period_end: _dt.datetime) -> dict:
        base = {
            "raw_usage_usd": str(self.raw_usage_usd),
            "grants_usd": str(self.grants_usd),
            "raw_usage_local": str(self.raw_usage_local),
            "grants_local": str(self.grants_local),
            "base_pricing_factor": str(self.base_pricing_factor),
            "overage_pricing_factor": str(self.overage_pricing_factor),
            "contract_usage_local": str(self.contract_usage_local),
            "payg_charge_local": str(self.payg_charge_local),
            "commit_charge_local": str(self.commit_charge_local),
            "overage_charge_local": str(self.overage_charge_local),
            "commit_amount": (
                str(self.commit_amount) if self.commit_amount is not None else None
            ),
            "monthly_commit_local": str(self.monthly_commit_local),
            "commit_schedule": self.commit_schedule,
            "is_commit_billing_period": self.is_commit_billing_period,
            "invoiced_local": str(self.invoiced_local),
            "currency": self.currency,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        }
        base.update(self.fx.to_audit_dict())
        return base

    # Back-compat shims for tests / call sites that read the old
    # USD-suffixed names. Kept until callers migrate.
    @property
    def fx_rate(self) -> Decimal:  # pragma: no cover - thin alias
        return self.fx.rate

    @property
    def fx_as_of_date(self) -> Optional[_dt.date]:  # pragma: no cover - thin alias
        return self.fx.as_of_date

    @property
    def invoiced_usd(self) -> Decimal:  # pragma: no cover - thin alias
        return self.invoiced_local if self.currency == "USD" else self.raw_usage_usd

    @property
    def contract_usage_usd(self) -> Decimal:  # pragma: no cover - thin alias
        return (
            self.contract_usage_local
            if self.currency == "USD"
            else self.contract_usage_local / self.fx.rate
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def invoice_metered_month(
    year: int | None = None,
    month: int | None = None,
    session: Session | None = None,
) -> MeteredInvoiceResult:
    """Invoice all eligible METERED accounts for the given period.

    Defaults to the *previous* month if ``year``/``month`` aren't passed
    (matching ``invoice_month`` semantics). Pass an explicit ``session``
    in tests; production callers omit it and the routine builds its own.
    """
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if year is None or month is None:
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - _dt.timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    period_start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc)
    period_end_exclusive = _next_month_start(period_start)

    if session is not None:
        return _invoice_metered_with_session(
            session,
            period_start=period_start,
            period_end_exclusive=period_end_exclusive,
        )

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as own_session:
        return _invoice_metered_with_session(
            own_session,
            period_start=period_start,
            period_end_exclusive=period_end_exclusive,
        )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def invoice_metered_month_for_account(
    billing_account_id: int,
    year: int,
    month: int,
    *,
    session: Session,
    force: bool = False,
) -> MeteredInvoiceResult:
    """Invoice exactly one METERED account for a given period.

    The single-account analogue of :func:`invoice_metered_month`.
    Operational tool for admins who need to retry one customer that
    failed in the bulk run, without re-scanning every eligible account.

    ``force=False`` (default) preserves the bulk-run idempotency check:
    if a recharge already exists for that (account, period), the call
    skips. Set ``force=True`` only after voiding the existing Stripe
    invoice and deleting the corresponding ``Recharge`` row by hand —
    this flag is for "I know what I'm doing" recovery scenarios.

    Raises ``ValueError`` if the account doesn't exist; otherwise
    returns a :class:`MeteredInvoiceResult` with counts of 1.

    NOTE: The session is NOT committed by this function — the caller
    (admin endpoint) decides when to commit so any auxiliary cleanup
    (Stripe void, Recharge purge) commits in the same transaction.
    """
    period_start = _dt.datetime(year, month, 1, tzinfo=_dt.timezone.utc)
    period_end_exclusive = _next_month_start(period_start)
    period_label = period_start.strftime("%Y-%m")
    invoice_group = month_end_utc(period_start.date())

    ba = session.get(BillingAccount, billing_account_id)
    if ba is None:
        raise ValueError(f"BillingAccount {billing_account_id} not found.")

    result = MeteredInvoiceResult(period=period_label)

    if force:
        # Caller asserts they've already cleaned up the prior invoice.
        # Drop the unique-key idempotency row so _process_one_account
        # doesn't short-circuit. We DON'T touch Stripe — voiding the
        # prior invoice is the operator's responsibility before calling.
        existing = _existing_metered_recharge(
            session,
            billing_account_id=ba.id,
            invoice_group=invoice_group,
        )
        if existing is not None:
            session.delete(existing)
            session.flush()

    from orchestra.lib.billing import configure_stripe

    configure_stripe()

    plan_dao = BillingPlanAssignmentDAO(session)
    try:
        _process_one_account(
            session=session,
            billing_account=ba,
            plan_dao=plan_dao,
            period_start=period_start,
            period_end_exclusive=period_end_exclusive,
            invoice_group=invoice_group,
            period_label=period_label,
            result=result,
        )
    except _SkipAccount as skip:
        logger.info(
            "Skipping metered re-run for account %s (%s): %s",
            ba.id,
            period_label,
            skip.reason,
        )
        result.accounts_skipped += 1
    return result


def _invoice_metered_with_session(
    session: Session,
    *,
    period_start: _dt.datetime,
    period_end_exclusive: _dt.datetime,
) -> MeteredInvoiceResult:
    """Run the full metered-invoicing pass within an existing session.

    One Stripe call per account; failures are isolated so one customer's
    Stripe outage doesn't block the rest of the run. Successful writes
    are persisted on the final ``session.commit()``.
    """
    period_label = period_start.strftime("%Y-%m")
    result = MeteredInvoiceResult(period=period_label)
    invoice_group = month_end_utc(period_start.date())

    eligible_accounts = _find_eligible_accounts(
        session,
        period_end_exclusive=period_end_exclusive,
    )
    if not eligible_accounts:
        return result

    # Drop the FX cache at the start of every bulk run so accidental
    # re-runs in the same Python process (e.g. a Cloud Run job that
    # failed mid-way and is rescheduled) re-fetch live rates rather
    # than reusing rates resolved in a possibly-stale earlier
    # attempt. Per-process caching for *within* a single run still
    # de-dupes the per-account network calls.
    from orchestra.lib.billing import configure_stripe
    from orchestra.lib.fx import reset_run_cache as _reset_fx_cache

    _reset_fx_cache()
    configure_stripe()

    plan_dao = BillingPlanAssignmentDAO(session)

    for ba in eligible_accounts:
        try:
            _process_one_account(
                session=session,
                billing_account=ba,
                plan_dao=plan_dao,
                period_start=period_start,
                period_end_exclusive=period_end_exclusive,
                invoice_group=invoice_group,
                period_label=period_label,
                result=result,
            )
        except _SkipAccount as skip:
            logger.info(
                "Skipping metered account %s for %s: %s",
                ba.id,
                period_label,
                skip.reason,
            )
            result.accounts_skipped += 1
        except Exception as exc:
            msg = (
                f"Failed to invoice metered account {ba.id} for "
                f"{period_label}: {type(exc).__name__}: {exc}"
            )
            logger.error(msg, exc_info=True)
            result.accounts_failed += 1
            result.errors.append(msg)

    session.commit()
    return result


# ---------------------------------------------------------------------------
# Per-account pipeline
# ---------------------------------------------------------------------------


class _SkipAccount(Exception):
    """Internal control-flow signal: skip this account, do not error."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _process_one_account(
    *,
    session: Session,
    billing_account: BillingAccount,
    plan_dao: BillingPlanAssignmentDAO,
    period_start: _dt.datetime,
    period_end_exclusive: _dt.datetime,
    invoice_group: _dt.date,
    period_label: str,
    result: MeteredInvoiceResult,
) -> None:
    """Compute, invoice (Stripe), and record one METERED account's period.

    Raises :class:`_SkipAccount` for soft skips (no Stripe customer,
    not on a METERED template, already invoiced for the period, nothing
    to bill) and re-raises any other exception for the caller to count
    as a failure.

    Suspension policy: a non-ACTIVE ``account_status`` does NOT cause a
    skip. We always invoice for actual ledger usage and the contract
    commit, regardless of status, because:

    * mid-period suspension (e.g. PAST_DUE on a previous invoice landing
      day-15 of the period) leaves real usage in the ledger that we
      delivered service for; silently dropping it = revenue loss + a
      "get suspended early to skip your bill" exploit;
    * the contract commit applies to the period the customer signed up
      for, regardless of when they were suspended — that's the standard
      enterprise interpretation;
    * if an operator wants to walk away from a specific invoice, they
      can void it in Stripe; the converse (invoicing after a skip) is
      operationally tedious (re-activate → re-run → re-suspend).

    The account's status and suspension_reason at invoice time are
    stamped into ``Recharge.detail`` for audit/dispute visibility, and
    a WARNING log line is emitted so on-call sees the unusual case.
    """
    if not billing_account.stripe_customer_id:
        raise _SkipAccount("no stripe_customer_id on file")

    # Use end-of-period (just before the next period) so a plan that
    # ended exactly at midnight still wins for this period.
    sample_moment = period_end_exclusive - _dt.timedelta(microseconds=1)
    assignment = plan_dao.get_in_force_at(billing_account.id, sample_moment)
    if assignment is None:
        raise _SkipAccount(
            "no assignment in force at period end (account was created after "
            "the period, or its history pre-dates the v2 backfill)",
        )
    template: BillingPlanTemplate = assignment.template
    if template.billing_mode != BillingMode.METERED.value:
        raise _SkipAccount(
            f"plan template billing_mode={template.billing_mode} (not METERED)",
        )

    # Idempotency: if we've already produced a recharge for this account
    # + period, skip. The unique combination is (billing_account_id,
    # type, invoice_group). This protects against accidental re-runs.
    existing = _existing_metered_recharge(
        session,
        billing_account_id=billing_account.id,
        invoice_group=invoice_group,
    )
    if existing is not None:
        raise _SkipAccount(
            f"already invoiced (recharge id={existing.id}, "
            f"stripe_invoice_id={existing.stripe_invoice_id})",
        )

    raw_usage_usd, grants_usd = _aggregate_period_ledger(
        session,
        billing_account_id=billing_account.id,
        period_start=period_start,
        period_end_exclusive=period_end_exclusive,
    )

    # Resolve FX rate for the period via the template's fx_policy.
    # Live-fetch errors (Frankfurter outage, etc.) become a per-account
    # skip so a flaky upstream doesn't sink the bulk run.
    try:
        resolved_fx = _resolve_fx_rate(
            template=template,
            period_start=period_start.date(),
            period_end_exclusive=period_end_exclusive.date(),
        )
    except FxProviderError as exc:
        raise _SkipAccount(
            f"FX rate USD->{template.currency} unavailable "
            f"(policy={template.fx_policy}): {exc}",
        )

    calc = _compute_invoice_line(
        template=template,
        raw_usage_usd=raw_usage_usd,
        grants_usd=grants_usd,
        resolved_fx=resolved_fx,
        period_start=period_start,
        assignment_started_at=assignment.started_at,
    )

    if calc.invoiced_local <= 0:
        raise _SkipAccount(
            f"computed invoice amount is {calc.invoiced_local} "
            f"{calc.currency} (no commit, no usage, or grants cancel usage)",
        )

    # ``period_label`` is the sortable internal form (e.g. ``2026-04``)
    # — used for idempotency keys, metadata, log lines. Customer-facing
    # strings (line descriptions + invoice memo) get the friendlier
    # full-month-year form.
    period_display = period_start.strftime("%B %Y")
    invoice_lines = _build_invoice_lines(template, period_display, calc)

    if billing_account.account_status != "ACTIVE":
        logger.warning(
            "Invoicing non-ACTIVE billing_account %s for %s: "
            "status=%s, suspension_reason=%s, amount=%s %s. "
            "Per current policy we always invoice for actual usage; "
            "void in Stripe if this invoice should not stand.",
            billing_account.id,
            period_label,
            billing_account.account_status,
            billing_account.suspension_reason,
            calc.invoiced_local,
            calc.currency,
        )

    invoice = _create_stripe_invoice(
        billing_account=billing_account,
        template=template,
        assignment=assignment,
        currency=calc.currency,
        lines=invoice_lines,
        invoice_group=invoice_group,
        period_label=period_label,
        period_display=period_display,
    )

    detail = calc.to_audit_dict(period_start, period_end_exclusive)
    detail["account_status_at_invoice"] = billing_account.account_status
    detail["suspension_reason_at_invoice"] = billing_account.suspension_reason

    recharge = Recharge(
        billing_account_id=billing_account.id,
        type=RECHARGE_TYPE_MONTHLY_COMMIT,
        # ``quantity`` is the invoice face value in the invoice currency.
        # ``amount_usd`` is the same figure converted back to USD so the
        # cross-currency reporting view doesn't have to do FX math.
        quantity=calc.invoiced_local,
        amount_usd=(
            calc.invoiced_local
            if calc.currency == "USD"
            else (calc.invoiced_local / calc.fx.rate).quantize(Decimal("0.01"))
        ),
        invoice_group=invoice_group,
        status=RechargeStatus.INVOICE_CREATED,
        stripe_invoice_id=invoice.id,
        plan_id=assignment.id,
        detail=detail,
    )
    session.add(recharge)

    INVOICE_CREATED_TOTAL.labels(
        entity_type="billing_account_metered",
        entity_id=str(billing_account.id),
    ).inc()
    result.accounts_invoiced += 1

    logger.info(
        "Metered invoice created for billing_account %s (%s): "
        "%s %s (raw_usage_usd=$%s, grants_usd=$%s, commit=%s, "
        "fx_policy=%s, fx_rate=%s). Invoice ID: %s",
        billing_account.id,
        period_label,
        calc.invoiced_local,
        calc.currency,
        calc.raw_usage_usd,
        calc.grants_usd,
        calc.commit_amount,
        calc.fx.policy,
        calc.fx.rate,
        invoice.id,
    )


def _resolve_fx_rate(
    *,
    template: BillingPlanTemplate,
    period_start: _dt.date,
    period_end_exclusive: _dt.date,
) -> _ResolvedFxRate:
    """Resolve the FX rate USD -> ``template.currency`` per the template policy.

    Dispatch table:

    * ``fx_policy IS NULL`` → USD template, no conversion needed; rate of 1.
    * ``LOCKED_RATE``       → use ``template.fx_locked_rate`` verbatim.
                              Validated by a check constraint to be present
                              and positive whenever this policy is set.
    * ``SPOT``              → live-fetch from Frankfurter (ECB-sourced)
                              for the LAST DAY of the billing period.
                              Last-day-of-period (rather than today) keeps
                              re-runs deterministic in practice — both the
                              original run and a same-week re-run get the
                              same Frankfurter rate.
    * ``PERIOD_AVERAGE``    → live-fetch a Frankfurter time-series across
                              the billing period and average the business-day
                              rates client-side. Smooths intra-month FX noise.

    Raises :class:`FxProviderError` for live-fetch failures (caught by
    the caller and converted to a per-account skip), or ``ValueError``
    for misconfigured templates that escape the check constraint
    (defensive — should never happen in practice).
    """
    policy = template.fx_policy
    target = (template.currency or "USD").upper()

    # USD templates have fx_policy=NULL — short-circuit with a no-op rate.
    if policy is None:
        return _ResolvedFxRate(
            rate=Decimal(1),
            policy="NONE",
            provider=None,
            as_of_date=None,
            period_start=None,
            period_end=None,
            sample_dates=None,
        )

    if policy == FxPolicy.LOCKED_RATE.value:
        if template.fx_locked_rate is None or template.fx_locked_rate <= 0:
            raise ValueError(
                f"Template {template.id} ({template.name!r}) has policy "
                "LOCKED_RATE but missing/invalid fx_locked_rate; the "
                "check constraint should have prevented this.",
            )
        return _ResolvedFxRate(
            rate=Decimal(str(template.fx_locked_rate)),
            policy=policy,
            provider=None,
            as_of_date=None,
            period_start=None,
            period_end=None,
            sample_dates=None,
        )

    # Period-end inclusive = day before period_end_exclusive.
    last_day = period_end_exclusive - _dt.timedelta(days=1)

    if policy == FxPolicy.SPOT.value:
        rate = fetch_spot(
            from_currency="USD",
            to_currency=target,
            as_of=last_day,
            provider=None,
        )
        return _ResolvedFxRate(
            rate=rate,
            policy=policy,
            provider="frankfurter",
            as_of_date=last_day,
            period_start=None,
            period_end=None,
            sample_dates=None,
        )

    if policy == FxPolicy.PERIOD_AVERAGE.value:
        average, sample_dates = fetch_period_average(
            from_currency="USD",
            to_currency=target,
            start=period_start,
            end=last_day,
            provider=None,
        )
        return _ResolvedFxRate(
            rate=average,
            policy=policy,
            provider="frankfurter",
            as_of_date=None,
            period_start=period_start,
            period_end=last_day,
            sample_dates=sample_dates,
        )

    raise ValueError(
        f"Template {template.id} ({template.name!r}) has unknown fx_policy "
        f"{policy!r}; please update _resolve_fx_rate.",
    )


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------


def _find_eligible_accounts(
    session: Session,
    *,
    period_end_exclusive: _dt.datetime,
) -> list[BillingAccount]:
    """Return all accounts that *might* be METERED for this period.

    Returns accounts that have at least one BillingPlanAssignment whose
    window covers any moment in the period (started_at < period_end AND
    (ended_at IS NULL OR ended_at > period_start)). The per-account
    pipeline then filters out ones whose plan-in-force at period end
    isn't actually METERED (e.g. switched to CREDITS mid-period).

    A second filter — only accounts whose plan template billing_mode is
    METERED — is applied at the plan-resolution step rather than here so
    the SQL stays simple and uses indexes we already have.
    """
    sample_moment = period_end_exclusive - _dt.timedelta(microseconds=1)
    period_start = period_end_exclusive - _dt.timedelta(days=1)
    # We want any account that had ANY assignment overlapping this period
    # AND whose assignment-in-force template was METERED. Easiest is to
    # join through the plan in force at sample_moment.
    rows = session.execute(
        select(BillingAccount)
        .join(
            BillingPlanAssignment,
            BillingPlanAssignment.billing_account_id == BillingAccount.id,
        )
        .join(
            BillingPlanTemplate,
            BillingPlanTemplate.id == BillingPlanAssignment.template_id,
        )
        .where(
            BillingPlanTemplate.billing_mode == BillingMode.METERED.value,
            BillingPlanAssignment.started_at <= sample_moment,
            or_(
                BillingPlanAssignment.ended_at.is_(None),
                BillingPlanAssignment.ended_at > sample_moment,
            ),
        )
        .distinct(),
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Ledger aggregation
# ---------------------------------------------------------------------------


def _aggregate_period_ledger(
    session: Session,
    *,
    billing_account_id: int,
    period_start: _dt.datetime,
    period_end_exclusive: _dt.datetime,
) -> tuple[Decimal, Decimal]:
    """Sum signed ledger amounts in the period.

    Returns ``(raw_usage_usd, grants_usd)`` both as positive Decimals:

    * ``raw_usage_usd`` = absolute value of the sum of negative amounts
      (everything ``deduct_credits`` wrote in METERED mode).
    * ``grants_usd`` = sum of positive amounts (everything ``add_credits``
      wrote — refunds, dispute credits, period grants, etc.).

    Pre-computed via two ``SUM(CASE ...)`` expressions so we make exactly
    one DB round-trip per account.
    """
    debits_expr = func.coalesce(
        func.sum(
            case(
                (CreditTransaction.amount < 0, -CreditTransaction.amount),
                else_=0,
            ),
        ),
        0,
    )
    grants_expr = func.coalesce(
        func.sum(
            case(
                (CreditTransaction.amount > 0, CreditTransaction.amount),
                else_=0,
            ),
        ),
        0,
    )
    row = session.execute(
        select(debits_expr, grants_expr).where(
            CreditTransaction.billing_account_id == billing_account_id,
            CreditTransaction.at >= period_start,
            CreditTransaction.at < period_end_exclusive,
        ),
    ).one()
    raw_usage_usd, grants_usd = row
    return Decimal(str(raw_usage_usd)), Decimal(str(grants_usd))


# ---------------------------------------------------------------------------
# Pricing formula
# ---------------------------------------------------------------------------


def _compute_invoice_line(
    *,
    template: BillingPlanTemplate,
    raw_usage_usd: Decimal,
    grants_usd: Decimal,
    resolved_fx: _ResolvedFxRate,
    period_start: _dt.datetime,
    assignment_started_at: Optional[_dt.datetime] = None,
) -> _LineCalculation:
    """Apply the plan formula to produce a final invoice amount.

    All arithmetic happens in the contract currency (``template.currency``).
    Raw USD figures coming in from the ledger are converted via
    ``resolved_fx.rate`` first; the formula then operates on ``_local``
    quantities, which is the currency Stripe will invoice in.

    Two pricing factors that **stack** on overage:

    * ``base_pricing_factor`` — applied to all usage uniformly:
      PAYG, the commit-included portion, and the overage portion.
      Set < 1.0 for a volume discount, > 1.0 for an above-list
      premium, 1.0 for list price.
    * ``overage_pricing_factor`` — an *additional* multiplier applied
      to the overage portion only, on top of ``base_pricing_factor``.
      Set 1.0 for "no overage penalty" (the base discount continues
      to apply); set > 1.0 to charge a premium for above-commit
      consumption (e.g. 1.25 = 25% uplift over the base rate). The
      effective rate above commit is ``base × overage``.

    Two independent COMMITMENT dimensions:

    * ``commit_period`` (MONTHLY/QUARTERLY/ANNUAL) sets the
      ``monthly_commit_local = commit_amount / months_in_period``
      floor used for overage. Overage is recomputed per month — a
      customer can't bank early-month underuse to cover late-month
      overage.
    * ``commit_schedule`` (AMORTISED/UPFRONT) controls *when* the
      commit dollars hit invoices. AMORTISED bills the per-month
      equivalent every month; UPFRONT bills the full ``commit_amount``
      on contract anniversaries (every ``months_in_period`` months
      from ``assignment_started_at``) and zero on intervening months.
      It never affects overage.

    Formula by quadrant:

    * **PAY_AS_YOU_GO** — ``contract_usage = raw_usage * base``;
      ``commit_charge = 0``, ``overage_charge = 0``.
    * **COMMITMENT, AMORTISED** — every month bills
      ``monthly_commit + overage`` where ``overage`` is
      ``max(0, raw_usage - monthly_commit/base) * overage_factor``.
    * **COMMITMENT, UPFRONT, anniversary month** — bills
      ``commit_amount + overage`` (full period commit + that month's
      overage).
    * **COMMITMENT, UPFRONT, non-anniversary month** — bills only
      ``overage``.

    ``assignment_started_at`` is required for COMMITMENT+UPFRONT
    plans (anniversary detection); the per-account caller passes
    ``BillingPlanAssignment.started_at``. Defaulted to ``None`` so PAYG
    callers and tests don't need to supply it. If absent for an
    UPFRONT plan we fall back to AMORTISED behaviour (defensive — a
    misconfigured caller still produces a sensible invoice instead of
    raising).

    Final ``invoiced_local = contract_usage_local - grants_local``.
    Negative results are clamped to 0 (we never owe the customer money
    via an invoice; goodwill credits roll forward via the ledger).

    For USD contracts, ``resolved_fx.rate=1`` and ``_local`` == ``_usd``.
    """
    currency = template.currency or "USD"
    fx_rate = resolved_fx.rate

    raw_usage_local = (
        raw_usage_usd if currency == "USD" else (raw_usage_usd * fx_rate).quantize(Decimal("0.01"))
    )
    grants_local = (
        grants_usd if currency == "USD" else (grants_usd * fx_rate).quantize(Decimal("0.01"))
    )

    base_factor = Decimal(str(template.base_pricing_factor))
    overage_factor = Decimal(str(template.overage_pricing_factor))

    commit_amount = (
        Decimal(str(template.commit_amount))
        if template.commit_amount is not None
        else None
    )

    # Plan-type ("PAYG vs COMMITMENT") is derived from the data: a
    # positive commit amount = COMMITMENT, anything else = PAYG.
    is_commitment = commit_amount is not None and commit_amount > 0

    if not is_commitment:
        # PAY_AS_YOU_GO — single rate, no commit, no overage,
        # no anniversary semantics.
        payg_charge_local = raw_usage_local * base_factor
        commit_charge_local = Decimal("0")
        overage_charge_local = Decimal("0")
        monthly_commit_local = Decimal("0")
        commit_schedule = None
        is_anniversary = False
    else:
        commit_amount_nn = commit_amount or Decimal("0")  # mypy guard
        months_per_period = _months_in_period(template.commit_period)

        # Per-month equivalent of the commit, in the contract currency.
        # Quantize to 2dp so monthly invoices stack to the contract total
        # without sub-cent drift (over a year a 12 × 833.333... ≈ $10000.00
        # rounds to a clean total).
        monthly_commit_local = (
            commit_amount_nn / Decimal(months_per_period)
        ).quantize(Decimal("0.01"))

        # Overage logic — identical regardless of schedule.
        # ``base_pricing_factor`` applies to ALL usage (commit-included
        # + overage); ``overage_pricing_factor`` is an *additional*
        # multiplier stacked on top for the overage portion only
        # (typically ``1.0`` = no penalty, ``> 1.0`` = premium uplift
        # over the base rate). So the effective overage rate is
        # ``base_factor × overage_factor``.
        included_capacity_local = (
            monthly_commit_local / base_factor if base_factor > 0 else Decimal("0")
        )
        overage_raw = raw_usage_local - included_capacity_local
        if overage_raw > 0:
            overage_charge_local = overage_raw * base_factor * overage_factor
        else:
            overage_charge_local = Decimal("0")

        # Commit-billing logic — depends on schedule.
        # Treat NULL / unknown schedule as AMORTISED for back-compat
        # with templates created before commit_schedule became a
        # required column.
        commit_schedule = template.commit_schedule or CommitSchedule.AMORTISED.value
        if commit_schedule == CommitSchedule.UPFRONT.value:
            if assignment_started_at is None:
                # Defensive: an UPFRONT plan should always be invoiced
                # via _process_one_account (which passes the
                # assignment). Fall back to AMORTISED if a caller
                # forgot — produces a sensible invoice rather than
                # raising. The misconfiguration shows up in
                # Recharge.detail (commit_schedule still says UPFRONT
                # but is_commit_billing_period is True every month).
                logger.warning(
                    "UPFRONT plan template %s invoiced without "
                    "assignment_started_at; falling back to AMORTISED. "
                    "This is a programming error — check the call site.",
                    template.id,
                )
                is_anniversary = True
            else:
                is_anniversary = _is_commit_billing_period(
                    started_at=assignment_started_at,
                    commit_period=template.commit_period,
                    period_start=period_start,
                )
            commit_charge_local = (
                commit_amount_nn if is_anniversary else Decimal("0")
            )
        else:
            # AMORTISED (or NULL → AMORTISED).
            is_anniversary = True
            commit_charge_local = monthly_commit_local

        payg_charge_local = Decimal("0")

    contract_usage_local = (
        payg_charge_local + commit_charge_local + overage_charge_local
    )
    invoiced_local = contract_usage_local - grants_local
    if invoiced_local < 0:
        invoiced_local = Decimal("0")

    return _LineCalculation(
        raw_usage_usd=raw_usage_usd,
        grants_usd=grants_usd,
        raw_usage_local=raw_usage_local,
        grants_local=grants_local,
        base_pricing_factor=base_factor,
        overage_pricing_factor=overage_factor,
        contract_usage_local=contract_usage_local,
        payg_charge_local=payg_charge_local,
        commit_charge_local=commit_charge_local,
        overage_charge_local=overage_charge_local,
        commit_amount=commit_amount,
        monthly_commit_local=monthly_commit_local,
        commit_schedule=commit_schedule,
        is_commit_billing_period=is_anniversary,
        invoiced_local=invoiced_local,
        currency=currency,
        fx=resolved_fx,
    )


@dataclass(frozen=True)
class _InvoiceLine:
    """One concrete Stripe ``InvoiceItem`` to create.

    Splitting the calculation into discrete lines lets the rendered
    invoice show the customer *what* they're paying for (monthly
    commitment vs. usage overage vs. credits applied) rather than a
    single opaque "metered invoice" total.

    ``kind`` is also embedded in the per-item idempotency key so a
    safe re-run of the invoicer can't accidentally reuse a key from a
    different line type — and so re-runs after a config change (e.g.
    the customer crosses into overage between two attempts) don't
    silently merge into a single Stripe item.
    """

    description: str
    amount: Decimal
    kind: str  # "commitment" | "overage" | "usage" | "credits_applied"


def _build_invoice_lines(
    template: BillingPlanTemplate,
    period_label: str,
    calc: _LineCalculation,
) -> List[_InvoiceLine]:
    """Decompose the computed total into per-line ``InvoiceItem``s.

    Cases (commit + overage are independent, so 0/1/2 lines per kind):

    * **COMMITMENT, AMORTISED** — every period emits a "monthly
      commitment" line at ``monthly_commit_local``. Periods with
      raw usage above the per-month floor add an "overage" line.
    * **COMMITMENT, UPFRONT, anniversary period** — emits an "annual
      / quarterly commitment" line at the *full* ``commit_amount``
      (the customer just rolled into a new contract period). Periods
      with overage in the same anniversary month also add an overage
      line.
    * **COMMITMENT, UPFRONT, non-anniversary period** — emits ONLY
      an overage line (commit was paid upfront on the anniversary,
      this month carries usage above the per-month floor only).
      If there's no overage either, no commit/overage lines are
      emitted (the upstream invoicer skips $0 invoices entirely).
    * **PAY_AS_YOU_GO METERED** — ONE line (raw usage at the base
      rate). Same as before.

    When the overage rate differs from the base rate, the overage
    line description carries the rate so the customer can see why
    above-commit usage costs more per unit. Mirrors the layout
    customers expect from enterprise invoices (Stripe, AWS, OpenAI
    all do the same).

    Grants are surfaced as a separate negative "Credits applied" line
    when present so the customer can see the gross-vs-net breakdown,
    matching the reference invoice we modelled this on.

    The customer-facing label uses ``template.display_name`` when set
    (falling back to ``template.name``) so internal slugs like
    ``vantage-overage-v3`` don't leak onto invoices.
    """
    label = template.display_name or template.name
    lines: List[_InvoiceLine] = []

    is_commitment = calc.commit_amount is not None and calc.commit_amount > 0

    if not is_commitment:
        # PAY_AS_YOU_GO — one line at the base rate.
        lines.append(
            _InvoiceLine(
                description=f"{label} — {period_label} (usage)",
                amount=calc.payg_charge_local,
                kind="usage",
            ),
        )
    else:
        # COMMITMENT — schedule-aware commit line, then overage if any.
        if calc.commit_charge_local > 0:
            # Word the commit line so AMORTISED reads "monthly
            # commitment" (per-month equivalent) and UPFRONT
            # anniversary periods read e.g. "annual commitment"
            # (the full period dollar). The same three values cover
            # all the non-MONTHLY periods cleanly.
            schedule = (
                calc.commit_schedule or CommitSchedule.AMORTISED.value
            )
            if schedule == CommitSchedule.UPFRONT.value:
                period_word = {
                    CommitPeriod.MONTHLY.value: "monthly",
                    CommitPeriod.QUARTERLY.value: "quarterly",
                    CommitPeriod.ANNUAL.value: "annual",
                }.get(template.commit_period or "", "period")
                commit_desc = (
                    f"{label} — {period_label} ({period_word} commitment)"
                )
            else:
                commit_desc = (
                    f"{label} — {period_label} (monthly commitment)"
                )
            lines.append(
                _InvoiceLine(
                    description=commit_desc,
                    amount=calc.commit_charge_local,
                    kind="commitment",
                ),
            )

        if calc.overage_charge_local > 0:
            # ``overage_pricing_factor`` stacks on top of base — when
            # it's > 1.0 the customer is paying a premium *over* the
            # base rate they signed up for, which is worth surfacing
            # on the line description so the higher per-unit cost is
            # explained at a glance. ``= 1.0`` means "same uplift as
            # base" (no extra penalty), which we keep terse.
            if calc.overage_pricing_factor != Decimal("1"):
                overage_desc = (
                    f"{label} — {period_label} "
                    f"(usage overage @ {calc.overage_pricing_factor}× base rate)"
                )
            else:
                overage_desc = f"{label} — {period_label} (usage overage)"
            lines.append(
                _InvoiceLine(
                    description=overage_desc,
                    amount=calc.overage_charge_local,
                    kind="overage",
                ),
            )

    if calc.grants_local > 0:
        lines.append(
            _InvoiceLine(
                description=f"Credits applied — {period_label}",
                amount=-calc.grants_local,
                kind="credits_applied",
            ),
        )

    return lines


# ---------------------------------------------------------------------------
# Stripe integration
# ---------------------------------------------------------------------------


def _create_stripe_invoice(
    *,
    billing_account: BillingAccount,
    template: BillingPlanTemplate,
    assignment: BillingPlanAssignment,
    currency: str,
    lines: List[_InvoiceLine],
    invoice_group: _dt.date,
    period_label: str,
    period_display: str,
):
    """Create one ``InvoiceItem`` per ``_InvoiceLine`` then finalize the Invoice.

    ``currency`` is the ISO-4217 code matching the template's
    ``currency`` (today USD or GBP). Stripe accepts the ISO code
    in lower-case; we coerce here to keep callers from worrying about
    it.

    For COMMITMENT plans crossing into overage we now create *two*
    InvoiceItems (commit floor + usage overage) so the rendered invoice
    breaks the charge down for the customer instead of bundling
    everything into one opaque "Metered invoice" line. Grants, when
    present, become a third negative-amount line. Floor-only and
    PAYG-METERED accounts still produce a single line.

    Idempotency keys include the assignment id AND the line ``kind`` so
    a reassign mid-month or a re-run after a calculation change can't
    silently overwrite a previously-recorded line. ``-invoice`` is
    reserved for the parent Invoice itself.
    """
    idem_base = (
        f"metered-ba-{billing_account.id}-{invoice_group}-asn-{assignment.id}"
    )

    # 1) One pending invoice item per line. ``stripe.Invoice.create``
    #    with ``pending_invoice_items_behavior=include`` will pull all
    #    of them in, in the order we created them, which gives us the
    #    "commit, overage, credits applied" rendering on the invoice.
    for line in lines:
        stripe.InvoiceItem.create(
            customer=billing_account.stripe_customer_id,
            amount=int(line.amount * 100),
            currency=currency.lower(),
            description=line.description,
            metadata={
                "billing_account_id": str(billing_account.id),
                "plan_assignment_id": str(assignment.id),
                "plan_template_id": str(template.id),
                "period": period_label,
                "invoice_group": str(invoice_group),
                "line_kind": line.kind,
            },
            idempotency_key=f"{idem_base}-{line.kind}-item",
        )

    # 2) Customer tax IDs, lifted from BillingAccount (matches the
    #    CREDITS-mode invoicer's behaviour exactly).
    customer_tax_ids = []
    if billing_account.tax_id:
        tax_id_type = billing_account.tax_id_type
        if not tax_id_type:
            country = None
            if billing_account.billing_address and isinstance(
                billing_account.billing_address,
                dict,
            ):
                country = billing_account.billing_address.get("country")
            tax_id_type = get_stripe_tax_id_type(country)
        customer_tax_ids = [{"type": tax_id_type, "value": billing_account.tax_id}]

    # ``currency`` is REQUIRED — without it, Stripe defaults to the
    # customer's / account's default currency, and
    # ``pending_invoice_items_behavior=include`` only pulls in pending
    # items whose currency matches the invoice's currency. Omitting
    # this on a GBP-template account whose customer's default currency
    # is USD would silently drop the just-created GBP InvoiceItem and
    # produce a $0 invoice. Always pin to the template's commit
    # currency so the InvoiceItem and the Invoice agree.
    invoice_params: dict = {
        "customer": billing_account.stripe_customer_id,
        "currency": currency.lower(),
        "automatic_tax": {"enabled": True},
        "auto_advance": True,
        "pending_invoice_items_behavior": "include",
        # Memo printed in the email body and on the hosted invoice page.
        # Intentionally generic — the per-line descriptions carry the
        # billing-mode specifics, and "Metered invoice for ..." reads
        # awkwardly to a customer who doesn't know our internal
        # CREDITS/METERED distinction. ``period_display`` is the
        # human-readable "April 2026" form (vs. the sortable ``2026-04``
        # used in metadata + idempotency keys).
        "description": f"Invoice for {period_display}",
        "metadata": {
            "invoice_group": str(invoice_group),
            "billing_account_id": str(billing_account.id),
            "plan_assignment_id": str(assignment.id),
            "plan_template_id": str(template.id),
            "plan_template_name": template.name,
            "plan_template_display_name": template.display_name or template.name,
            "period": period_label,
            "billing_mode": template.billing_mode,
        },
    }

    # Collection method dispatch + per-method payment_settings.
    payment_method_types = _resolve_payment_method_types(
        billing_account=billing_account,
        template=template,
        currency=currency,
    )
    payment_settings: dict = {
        "payment_method_types": payment_method_types,
        "payment_method_options": _payment_method_options(
            payment_method_types,
            currency=currency,
            billing_account=billing_account,
        ),
    }

    if template.collection_method == CollectionMethod.SEND_INVOICE_NET_30.value:
        invoice_params["collection_method"] = "send_invoice"
        invoice_params["days_until_due"] = 30
    else:
        # AUTO_CARD — Stripe default. Set explicitly so reading the
        # invoice in the dashboard shows our intent.
        invoice_params["collection_method"] = "charge_automatically"

    invoice_params["payment_settings"] = payment_settings

    if customer_tax_ids:
        invoice_params["customer_tax_ids"] = customer_tax_ids

    return stripe.Invoice.create(
        **invoice_params,
        idempotency_key=f"{idem_base}-invoice",
    )


# ---------------------------------------------------------------------------
# customer_balance bank-transfer rails — closed Stripe-defined set.
#
# Stripe supports exactly five bank-transfer funding types for the
# ``customer_balance`` payment method, each tied to a single currency
# (the EU type additionally requires a country code from a small
# whitelist). Currencies not listed here cannot be funded via
# ``customer_balance`` — the invoicer falls back to ``card`` for those
# rather than failing the whole run for the account.
#
# Ref: https://docs.stripe.com/payments/customer-balance and
# Stripe API ``customer_cash_balance.bank_transfer.type``. Adding a
# rail also requires enabling the matching funding type in the Stripe
# Dashboard (Settings → Payments → Customer balance) before invoices
# referencing it will succeed at runtime.
# ---------------------------------------------------------------------------

# Currency (ISO 4217, lowercase) → Stripe ``bank_transfer.type`` value.
_BANK_TRANSFER_TYPE_BY_CURRENCY: dict[str, str] = {
    "usd": "us_bank_transfer",
    "gbp": "gb_bank_transfer",
    "eur": "eu_bank_transfer",
    "jpy": "jp_bank_transfer",
    "mxn": "mx_bank_transfer",
}

# Country whitelist for ``eu_bank_transfer`` — Stripe only issues a
# virtual IBAN for customers domiciled in one of these EU countries.
# Other EU customers must use card.
_EU_BANK_TRANSFER_COUNTRIES: frozenset[str] = frozenset(
    {"BE", "DE", "ES", "FR", "IE", "NL"},
)


def _account_billing_country(billing_account: BillingAccount) -> Optional[str]:
    """Return the ISO-3166 alpha-2 country from ``billing_address``, if any."""
    address = billing_account.billing_address
    if not isinstance(address, dict):
        return None
    country = address.get("country")
    if isinstance(country, str) and country.strip():
        return country.strip().upper()
    return None


def _bank_transfer_options(
    *,
    currency: str,
    billing_account: BillingAccount,
) -> Optional[dict]:
    """Return the ``customer_balance`` options dict for *currency*, or None.

    ``None`` means "this currency cannot be funded via customer_balance"
    — typically because Stripe doesn't offer a bank-transfer rail for
    that currency, or because EUR is requested without a supported
    billing country. Callers drop ``customer_balance`` from
    ``payment_method_types`` in that case so the invoice still goes
    out (card-only).
    """
    bank_transfer_type = _BANK_TRANSFER_TYPE_BY_CURRENCY.get(currency.lower())
    if bank_transfer_type is None:
        return None
    bank_transfer: dict = {"type": bank_transfer_type}
    if bank_transfer_type == "eu_bank_transfer":
        country = _account_billing_country(billing_account)
        if country is None or country not in _EU_BANK_TRANSFER_COUNTRIES:
            # Stripe rejects the invoice without a supported country;
            # fall back to card-only rather than failing the run.
            return None
        bank_transfer["eu_bank_transfer"] = {"country": country}
    return {
        "funding_type": "bank_transfer",
        "bank_transfer": bank_transfer,
    }


def _resolve_payment_method_types(
    *,
    billing_account: BillingAccount,
    template: BillingPlanTemplate,
    currency: str,
) -> List[str]:
    """Pick the ``payment_method_types`` for a Stripe Invoice.

    Precedence:

    1. ``BillingAccount.preferred_payment_method_types`` — explicit
       per-customer override set via the admin endpoint. Honoured
       verbatim (already validated at write time by
       ``BillingAccountDAO.set_payment_preferences``) — the operator
       took responsibility when they set it, so we don't second-guess
       (a wire-only override on an unsupported currency will fail at
       invoice creation, which is the operator's signal to fix it).
    2. Per-``CollectionMethod`` default:

       * AUTO_CARD → ``['card']`` only. The customer's saved card is
         the only thing Stripe should attempt; ``customer_balance`` is
         a *push* method and would be meaningless on a
         ``charge_automatically`` invoice.
       * SEND_INVOICE_NET_30 → ``['card', 'customer_balance']`` when
         ``customer_balance`` is supported for the invoice's currency
         (and for EUR, the customer's billing country); otherwise
         ``['card']`` alone. We never raise here — falling back to
         card keeps the customer's invoice deliverable; the loss of
         the wire option is logged so ops notice when a new currency
         needs a Dashboard funding-type enabled.
    """
    if billing_account.preferred_payment_method_types:
        return list(billing_account.preferred_payment_method_types)
    if template.collection_method != CollectionMethod.SEND_INVOICE_NET_30.value:
        return [PaymentMethodType.CARD.value]
    if _bank_transfer_options(currency=currency, billing_account=billing_account) is None:
        logger.warning(
            "customer_balance unsupported for billing_account=%s currency=%s "
            "country=%s; falling back to card-only on this invoice. Add a "
            "currency mapping in monthly_metered_invoicer if a Stripe "
            "bank-transfer rail is now available.",
            billing_account.id,
            currency,
            _account_billing_country(billing_account),
        )
        return [PaymentMethodType.CARD.value]
    return [PaymentMethodType.CARD.value, PaymentMethodType.CUSTOMER_BALANCE.value]


def _payment_method_options(
    payment_method_types: List[str],
    *,
    currency: str,
    billing_account: BillingAccount,
) -> dict:
    """Build ``Invoice.payment_settings.payment_method_options``.

    Centralised so the invoicer never silently drops a required option
    (e.g. ``customer_balance`` *requires* a ``funding_type`` and a
    matching ``bank_transfer.type`` — without them Stripe rejects the
    invoice with an obscure error and the run fails for that account).

    Picking the wrong rail silently routes the customer's wire to the
    wrong virtual account, so the mapping in
    ``_BANK_TRANSFER_TYPE_BY_CURRENCY`` is the single source of truth
    and is consulted both here and in
    ``_resolve_payment_method_types``. This function still raises if
    a caller explicitly forces ``customer_balance`` on an unsupported
    currency (e.g. via ``preferred_payment_method_types``) — surfacing
    that as a per-account failure is preferable to silently dropping
    the option from an explicit override.
    """
    options: dict = {}
    if PaymentMethodType.CARD.value in payment_method_types:
        # 3DS for SCA compliance on EU/UK cards; harmless for US.
        options["card"] = {"request_three_d_secure": "any"}
    if PaymentMethodType.CUSTOMER_BALANCE.value in payment_method_types:
        bank_transfer = _bank_transfer_options(
            currency=currency,
            billing_account=billing_account,
        )
        if bank_transfer is None:
            raise ValueError(
                f"customer_balance is not configured for currency "
                f"{currency.lower()!r} on billing_account "
                f"{billing_account.id} (country="
                f"{_account_billing_country(billing_account)!r}); add a "
                "bank-transfer rail in monthly_metered_invoicer or remove "
                "'customer_balance' from the customer's "
                "preferred_payment_method_types.",
            )
        options["customer_balance"] = bank_transfer
    return options


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _existing_metered_recharge(
    session: Session,
    *,
    billing_account_id: int,
    invoice_group: _dt.date,
) -> Recharge | None:
    """Look up a previously-created metered recharge for this account+period."""
    return (
        session.execute(
            select(Recharge).where(
                and_(
                    Recharge.billing_account_id == billing_account_id,
                    Recharge.invoice_group == invoice_group,
                    Recharge.type == RECHARGE_TYPE_MONTHLY_COMMIT,
                ),
            ),
        )
        .scalars()
        .first()
    )


def _next_month_start(day: _dt.datetime) -> _dt.datetime:
    """Return the first instant of the month following *day* (UTC)."""
    if day.month == 12:
        return day.replace(year=day.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return day.replace(month=day.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# In-progress estimate (powers the customer-facing usage progress bar)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InProgressInvoiceEstimate:
    """Mid-period invoice projection for a METERED account.

    Backs the progress bar on the customer billing page. Quantities are
    in the contract currency (``currency``); for USD templates that's
    just USD. ``period_start`` is inclusive and ``period_end_exclusive``
    is exclusive — both UTC-aware and aligned to month boundaries.

    Three commit-related fields, each answering a different UX question:

    * ``commit_amount`` — the contract-period commit (e.g. $12000 for
      an annual ANNUAL contract). The "what they signed up for" total.
    * ``monthly_commit_local`` — the per-month equivalent
      (``commit_amount / months_in_period``). The "right denominator
      for this month's progress bar" — the same value regardless of
      ``commit_period`` so a customer with $12k/year sees usage
      against $1k/mo, just like a customer with $1k/mo.
    * ``commit_charge_local`` — what the commit-line on *this* month's
      invoice will be. AMORTISED: equals ``monthly_commit_local``.
      UPFRONT: equals ``commit_amount`` on anniversary months and
      ``0`` otherwise. Lets the UI distinguish "you're using your
      contract" (always ``monthly_commit_local``) from "what you'll
      be invoiced" (``commit_charge_local + overage_local``).

    ``overage_local`` is the portion above the per-month floor
    (always 0 for PAYG and for COMMITMENT periods that haven't burned
    through this month's floor yet).

    Mid-period FX is best-effort: SPOT and PERIOD_AVERAGE policies are
    resolved against the partial period (start → today). The finalized
    invoice at period close uses the full-period rate, which can move
    this estimate slightly.
    """

    period_start: _dt.datetime
    period_end_exclusive: _dt.datetime
    currency: str
    raw_usage_local: Decimal
    contract_usage_local: Decimal
    commit_amount: Optional[Decimal]
    monthly_commit_local: Decimal
    commit_charge_local: Decimal
    invoiced_estimate_local: Decimal
    overage_local: Decimal
    commit_schedule: Optional[str]
    is_commit_billing_period: bool


def estimate_in_progress_invoice(
    session: Session,
    *,
    billing_account_id: int,
    as_of: Optional[_dt.datetime] = None,
) -> Optional[InProgressInvoiceEstimate]:
    """Project what the current period's METERED invoice would look like.

    Returns ``None`` if the account's active plan is not METERED — the
    caller (customer billing endpoint) treats that as "not applicable"
    and renders a credits view instead.

    The aggregation window is the full calendar month containing
    ``as_of`` (UTC); FX is resolved against the partial window
    ``[period_start, as_of_day + 1)`` so SPOT picks up today's quote
    and PERIOD_AVERAGE smooths only the elapsed portion of the month.
    """

    now = as_of or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)

    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_end_exclusive = _next_month_start(period_start)

    plan_dao = BillingPlanAssignmentDAO(session)
    plan = plan_dao.resolve_effective_plan(billing_account_id)
    if plan.billing_mode != "METERED":
        return None

    template = (
        session.execute(
            select(BillingPlanTemplate).where(
                BillingPlanTemplate.id == plan.template_id,
            ),
        )
        .scalar_one()
    )

    # The active assignment carries the contract anniversary date used
    # by UPFRONT-schedule plans to decide whether this month's invoice
    # includes the commit charge or only the overage. AMORTISED + PAYG
    # ignore it so we tolerate a missing assignment gracefully.
    active_assignment = plan_dao.get_active(billing_account_id)
    assignment_started_at = (
        active_assignment.started_at if active_assignment is not None else None
    )

    raw_usage_usd, grants_usd = _aggregate_period_ledger(
        session,
        billing_account_id=billing_account_id,
        period_start=period_start,
        period_end_exclusive=period_end_exclusive,
    )

    # Resolve FX against the elapsed portion of the period so SPOT /
    # PERIOD_AVERAGE land on a sensible mid-month rate. ``+1 day`` so
    # the existing helper's ``last_day = end - 1`` lands on today.
    fx_window_end = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    try:
        resolved_fx = _resolve_fx_rate(
            template=template,
            period_start=period_start.date(),
            period_end_exclusive=fx_window_end.date(),
        )
    except Exception:  # noqa: BLE001
        # Live FX fetch can fail (network, provider down). Fall back to
        # 1.0 — the estimate just shows raw USD numbers labelled in the
        # contract currency, which is wrong but recoverable; better
        # than 500ing the whole billing page.
        logger.warning(
            "FX resolution failed for in-progress estimate (account=%s); "
            "falling back to rate=1.0",
            billing_account_id,
            exc_info=True,
        )
        resolved_fx = _ResolvedFxRate(
            rate=Decimal(1),
            policy="NONE",
            provider=None,
            as_of_date=None,
            period_start=None,
            period_end=None,
            sample_dates=None,
        )

    line = _compute_invoice_line(
        template=template,
        raw_usage_usd=raw_usage_usd,
        grants_usd=grants_usd,
        resolved_fx=resolved_fx,
        period_start=period_start,
        assignment_started_at=assignment_started_at,
    )

    # ``contract_usage_local`` already reflects the schedule-aware
    # commit charge + overage (the formula split lives in
    # ``_compute_invoice_line``); the estimate just surfaces both
    # decompositions so the UI can render "this month" (the
    # invoice estimate) and "your contract" (the per-month
    # capacity) without recomputing.
    return InProgressInvoiceEstimate(
        period_start=period_start,
        period_end_exclusive=period_end_exclusive,
        currency=line.currency,
        raw_usage_local=line.raw_usage_local,
        contract_usage_local=line.contract_usage_local,
        commit_amount=line.commit_amount,
        monthly_commit_local=line.monthly_commit_local,
        commit_charge_local=line.commit_charge_local,
        invoiced_estimate_local=line.contract_usage_local,
        overage_local=line.overage_charge_local,
        commit_schedule=line.commit_schedule,
        is_commit_billing_period=line.is_commit_billing_period,
    )
