"""DAO for ``BillingPlanAssignment`` — time-bounded plan assignments.

A ``BillingPlanAssignment`` row represents one phase of one account's plan
history. The DAO encapsulates four invariants that callers should never have
to reason about:

1. **Every account always has exactly one active assignment.** Pristine
   self-serve accounts get a default plan assignment at signup
   (inserted by ``BillingAccountDAO.create``, backfilled by the
   ``metered_and_billing_plan`` migration for accounts
   that pre-date v2). There is no "pristine = no assignment" shape;
   the default plan has the same representation as any other
   plan. Note that ``BillingAccount.plan_assignment_id`` is *nullable in
   the DB* (PostgreSQL ``NOT NULL`` is not deferrable, which would
   create a chicken-and-egg at row-creation time) but **NOT NULL by
   application contract** — any NULL pointer in production is
   corruption that the reconciliation routine surfaces as critical.
2. **At most one active assignment per account** at any moment. Enforced
   by a partial unique index in the DB; this DAO never tries to create a
   second active row.
3. **Plan changes leave a closed row + an open row.** ``set_plan()`` ends
   the previous active row and inserts a new one with the same
   ``effective_at`` boundary so history has no gap and no overlap.
   This applies uniformly to assigning a custom contract,
   changing between two custom contracts, and cancelling back to
   default (which inserts a fresh default plan row — there is no
   "clear the pointer" code path). There is intentionally **no**
   per-row supersedes pointer — history is reconstructed by
   ``started_at DESC`` order on the same account, and each row's
   ``change_reason`` documents *why* it took over from the previous
   one.
4. **The denormalised ``BillingAccount.plan_assignment_id`` pointer stays in
   sync.** Every ``set_plan`` updates it in the same DB transaction.

Settlement of in-flight balances at transition (negative wallet on a
CREDITS→METERED switch, unbilled usage on a METERED→CREDITS switch) is
NOT this DAO's responsibility. The admin endpoint orchestrates that via
add_credits() / deduct_credits() before calling set_plan().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    DEFAULT_TEMPLATE_ID,
    BillingAccount,
    BillingPlanAssignment,
    BillingPlanTemplate,
    Recharge,
    RechargeStatus,
)


class TemplateNotAssignableError(Exception):
    """Raised when trying to assign an inactive (deprecated) template.

    Custom templates with ``is_active=true`` remain assignable — they're
    hand-crafted for one customer and being hidden from the public
    catalog (``is_custom=true``) is by design.
    """


class ConcurrentPlanChangeError(Exception):
    """Raised when two ``set_plan`` calls race on the same account.

    Both readers see "no open assignment in conflict" (the existing
    open row is the one they intend to close) and both try to insert a
    new active row. The DB partial unique index
    ``ux_billing_plan_assignment_active_unique`` rejects the second
    insert with an ``IntegrityError`` — we translate it here so the
    admin endpoint can return a structured 409 rather than letting a
    generic 500 escape.

    The caller is expected to read the new active assignment (the
    winner of the race) and decide whether their intended switch is
    now a no-op or still needed; this DAO does not retry.
    """

    def __init__(self, billing_account_id: int) -> None:
        self.billing_account_id = billing_account_id
        super().__init__(
            f"Concurrent set_plan for billing_account {billing_account_id}: "
            "another writer changed the active assignment between the read "
            "and the insert. Refetch the active plan and retry if the "
            "intended template is still different.",
        )


class PendingRechargesError(Exception):
    """Raised when ``set_plan`` would strand in-flight CREDITS recharges.

    A ``Recharge`` row in ``PENDING_INVOICE`` represents money the
    customer owes for credits already granted on the wallet — it has
    not yet been rolled into a Stripe invoice (that happens at
    month-end via ``monthly_credits_invoicer``). Switching the account
    to a different plan (typically CREDITS → METERED) before that
    settles would silently orphan the row: the credits invoicer would
    historically filter it out by live account mode, and the metered
    invoicer never touches CREDITS-mode recharges. The customer would
    walk away with the credits without paying for them.

    Operators have two clean paths once they see this error:

    1. Wait for the next month-end run (the recharge will invoice
       under the prior plan and resolve via webhook), then retry.
    2. Manually drain the row — void via Stripe + mark FAILED, or
       force-bill via the admin invoice-month endpoint — and retry.

    The error carries the offending recharge ids in
    ``pending_recharge_ids`` so callers (admin endpoint, customer
    self-serve switch) can surface them in the 409 response.
    """

    def __init__(self, billing_account_id: int, pending_recharge_ids: List[int]):
        self.billing_account_id = billing_account_id
        self.pending_recharge_ids = pending_recharge_ids
        super().__init__(
            f"BillingAccount {billing_account_id} has "
            f"{len(pending_recharge_ids)} pending recharge(s) waiting to be "
            f"invoiced (recharge ids={pending_recharge_ids}). Drain or wait "
            "for the next monthly_credits_invoicer run before switching plans.",
        )


@dataclass(frozen=True)
class EffectivePlan:
    """Fully-resolved billing plan for one account at one moment.

    Combines the active assignment with its template. Every account has
    an active assignment under the v2 invariant (default by
    default, inserted at signup by ``BillingAccountDAO.create``), so
    ``assignment_id`` is always populated.

    Decimals are preserved end-to-end. Callers that serialise to JSON
    (``CurrentPlanSummary`` in the view layer) coerce to ``float`` at
    the boundary; callers that do arithmetic (the metered invoicer)
    keep them as Decimals.

    Plan-type ("PAYG" vs "COMMITMENT") is derived from
    ``commit_amount`` — there is no separate stored value. Callers that
    need a label use ``"COMMITMENT" if commit_amount and commit_amount
    > 0 else "PAY_AS_YOU_GO"`` directly.
    """

    assignment_id: int
    template_id: int
    template_name: str
    # Customer-facing label; falls back to ``template_name`` when the
    # template row has no ``display_name`` set.
    template_display_name: str
    billing_mode: str
    commit_amount: Optional[Decimal]
    currency: str
    commit_period: Optional[str]
    commit_schedule: Optional[str]
    collection_method: str
    base_pricing_factor: Decimal
    overage_pricing_factor: Decimal
    started_at: Optional[datetime]
    ended_at: Optional[datetime]


class BillingPlanAssignmentDAO:
    """Manage plan assignments for a billing account."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_by_id(self, assignment_id: int) -> Optional[BillingPlanAssignment]:
        return self.session.get(BillingPlanAssignment, assignment_id)

    def get_active(self, billing_account_id: int) -> Optional[BillingPlanAssignment]:
        """Return the assignment in force at "now".

        Under the v2 invariant every account has exactly one in-force row
        — pristine accounts get a default assignment at signup, and
        ``set_plan`` always closes-and-inserts. ``None`` therefore
        indicates schema corruption (the reconciliation routine flags
        this) and the caller should treat it as such; production code
        should never see it.

        The filter is ``started_at <= now() < (ended_at OR +∞)`` so that
        a row scheduled at a future boundary by ``switch_plan``
        (AT_BOUNDARY policy) does *not* prematurely shadow the still-
        in-force current assignment. ``get_in_force_at(t)`` is the
        general-time variant; this is the "now" specialization.
        """
        now = datetime.utcnow()
        return (
            self.session.execute(
                select(BillingPlanAssignment)
                .where(
                    BillingPlanAssignment.billing_account_id == billing_account_id,
                    BillingPlanAssignment.started_at <= now,
                    (BillingPlanAssignment.ended_at.is_(None))
                    | (BillingPlanAssignment.ended_at > now),
                )
                .order_by(BillingPlanAssignment.started_at.desc())
                .limit(1),
            )
            .scalars()
            .first()
        )

    def get_in_force_at(
        self,
        billing_account_id: int,
        moment: datetime,
    ) -> Optional[BillingPlanAssignment]:
        """Return the assignment that was in force at a specific moment in time.

        Used by the metered invoicer to attribute usage rows to the right
        plan version when reconstructing past invoices. Returns ``None``
        when no assignment row covers ``moment`` (which can happen for
        time windows before the account had any assignment — e.g.
        before its very first signup row, or for accounts whose
        history pre-dates the v2 migration backfill).
        """
        return (
            self.session.execute(
                select(BillingPlanAssignment)
                .where(
                    BillingPlanAssignment.billing_account_id == billing_account_id,
                    BillingPlanAssignment.started_at <= moment,
                    (BillingPlanAssignment.ended_at.is_(None))
                    | (BillingPlanAssignment.ended_at > moment),
                )
                .order_by(BillingPlanAssignment.started_at.desc())
                .limit(1),
            )
            .scalars()
            .first()
        )

    def list_history(
        self,
        billing_account_id: int,
        *,
        limit: int = 100,
    ) -> List[BillingPlanAssignment]:
        """Return all assignments for an account, newest first."""
        return list(
            self.session.execute(
                select(BillingPlanAssignment)
                .where(BillingPlanAssignment.billing_account_id == billing_account_id)
                .order_by(BillingPlanAssignment.started_at.desc())
                .limit(limit),
            )
            .scalars()
            .all(),
        )

    def resolve_effective_plan(self, billing_account_id: int) -> EffectivePlan:
        """Return the fully-resolved plan for an account.

        Single source of truth for "what plan does this account have,
        right now?" — every account has an active assignment (Default
        PAYG at minimum, backfilled at migration time and inserted at
        signup by ``BillingAccountDAO.create``), so this always returns
        a real ``EffectivePlan`` backed by a real assignment row.

        Raises ``RuntimeError`` if no active assignment is found.
        That's an application-invariant violation (the column is
        nullable in the DB so the schema can't enforce it, but the
        DAO + migration backfill make it impossible in practice) and
        the daily reconciliation routine flags it as critical so it
        gets fixed promptly. Production code should never see this.
        """
        active = self.get_active(billing_account_id)
        if active is None:
            raise RuntimeError(
                f"BillingAccount {billing_account_id} has no active "
                "BillingPlanAssignment — application invariant violated. "
                "Every account is supposed to have at least one open "
                "row (the default plan). Investigate via the "
                "billing reconciliation routine; the account-creation "
                "flow or a manual SQL op probably skipped the initial "
                "assignment insert.",
            )

        template = active.template
        return EffectivePlan(
            assignment_id=active.id,
            template_id=template.id,
            template_name=template.name,
            template_display_name=template.display_name or template.name,
            billing_mode=template.billing_mode,
            commit_amount=(
                Decimal(str(template.commit_amount))
                if template.commit_amount is not None
                else None
            ),
            currency=template.currency,
            commit_period=template.commit_period,
            commit_schedule=template.commit_schedule,
            collection_method=template.collection_method,
            base_pricing_factor=Decimal(str(template.base_pricing_factor)),
            overage_pricing_factor=Decimal(str(template.overage_pricing_factor)),
            started_at=_ensure_utc(active.started_at),
            ended_at=_ensure_utc(active.ended_at),
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def assign_default_at_signup(
        self,
        billing_account_id: int,
    ) -> BillingPlanAssignment:
        """Insert the initial default plan assignment for a new account.

        Called once per ``BillingAccount`` from the account-creation
        flow, immediately after ``session.flush()`` makes the account
        id available. Establishes the v2 invariant: every account has
        an active assignment from the moment it exists.

        Idempotent: if an active assignment already exists for the
        account (e.g. the migration backfill ran for an older account),
        returns the existing row instead of inserting a duplicate.
        """
        existing = self.get_active(billing_account_id)
        if existing is not None:
            return existing
        default = self._require_assignable_template(DEFAULT_TEMPLATE_ID)
        return self._insert_active_assignment(
            billing_account_id=billing_account_id,
            template=default,
            created_by_user_id=None,
            change_reason="default plan (initial assignment at signup)",
            started_at=datetime.utcnow(),
        )

    def set_plan(
        self,
        *,
        billing_account_id: int,
        template_id: int,
        created_by_user_id: Optional[str] = None,
        change_reason: Optional[str] = None,
        effective_at: Optional[datetime] = None,
    ) -> Optional[BillingPlanAssignment]:
        """Set the active plan for an account, atomically and uniformly.

        One method covers the three transitions formerly modelled as
        ``assign`` / ``change_plan`` / ``cancel``:

        * **default → custom template** — close the active default
          row at ``effective_at`` and insert the new template's row
          starting at the same moment.
        * **template A → template B** — same mechanics; close + insert.
        * **custom → DEFAULT_TEMPLATE ("cancel")** — close the active row
          and insert a *fresh* default plan assignment row. The new
          row's ``change_reason`` documents *why* (e.g. ``"Cancelled
          enterprise contract"``).

        Returns the newly-created ``BillingPlanAssignment``, or
        ``None`` if the account is already on ``template_id`` (idempotent
        no-op — neither closes nor re-inserts).

        ``effective_at`` defaults to "now" but admin endpoints typically
        pass the next billing-period boundary to honour AT_BOUNDARY
        policy. Both the close timestamp and the new ``started_at`` use
        this value so the history is contiguous (no gap, no overlap).

        Raises ``PendingRechargesError`` when the account has
        ``Recharge`` rows in ``PENDING_INVOICE`` — see the exception's
        docstring for why we refuse rather than try to settle them.
        ``INVOICE_CREATED`` rows are *not* a blocker: those are
        already collected via Stripe and resolve via webhook
        independent of plan state.

        Raises ``ConcurrentPlanChangeError`` when two writers race on
        the same account — the second insert hits the partial unique
        index ``ux_billing_plan_assignment_active_unique`` and we
        translate the resulting ``IntegrityError`` so callers see a
        domain-specific error (and the API can return 409) instead of
        a generic 500.
        """
        active = self.get_active(billing_account_id)
        if active is None:
            # Schema invariant violation — every account is supposed to
            # have an active row from signup. Refuse to silently paper
            # over it; the reconciliation routine will flag this for
            # operator follow-up.
            raise RuntimeError(
                f"BillingAccount {billing_account_id} has no active "
                "BillingPlanAssignment. The account-creation flow or a "
                "manual SQL op skipped the initial default plan insert; "
                "fix the data via reconciliation before retrying.",
            )
        if active.template_id == template_id:
            return None
        new_template = self._require_assignable_template(template_id)
        # Guard: refuse to switch while CREDITS-side recharges are
        # still pending invoice. See ``PendingRechargesError`` for the
        # exploit this prevents (auto-recharge top-up + same-period
        # switch to METERED would otherwise strand the row).
        pending_ids = self._pending_recharge_ids(billing_account_id)
        if pending_ids:
            raise PendingRechargesError(billing_account_id, pending_ids)
        moment = effective_at or datetime.utcnow()
        active.ended_at = moment
        try:
            self.session.flush()
            return self._insert_active_assignment(
                billing_account_id=billing_account_id,
                template=new_template,
                created_by_user_id=created_by_user_id,
                change_reason=change_reason,
                started_at=moment,
            )
        except IntegrityError as exc:
            # Partial unique index ``ux_billing_plan_assignment_active_unique``
            # — only fired when another writer concurrently closed the
            # same active row and inserted its own. Anything else
            # (e.g. FK violation on template_id) we re-raise unchanged
            # since the error name will be different and we don't want
            # to mask genuine bugs.
            constraint = (
                getattr(exc.orig, "diag", None)
                and getattr(exc.orig.diag, "constraint_name", None)
            ) or ""
            if "ux_billing_plan_assignment_active_unique" in (
                constraint or str(exc.orig)
            ):
                # Roll back so the session is usable again — the
                # caller's outer transaction can either retry or
                # surface 409 to the client.
                self.session.rollback()
                raise ConcurrentPlanChangeError(billing_account_id) from exc
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _pending_recharge_ids(self, billing_account_id: int) -> List[int]:
        """Return ids of ``PENDING_INVOICE`` recharges for an account.

        Scoped by billing account (not by assignment) because CREDITS
        auto-recharge writes ``Recharge.plan_id = NULL`` by invariant
        — there is no assignment FK to filter on. The credits invoicer
        is the single writer of the ``PENDING_INVOICE → INVOICE_CREATED``
        transition; ``set_plan`` defers to it rather than trying to
        force-settle in-band.
        """
        return list(
            self.session.execute(
                select(Recharge.id).where(
                    Recharge.billing_account_id == billing_account_id,
                    Recharge.status == RechargeStatus.PENDING_INVOICE,
                ),
            )
            .scalars()
            .all(),
        )

    def _require_assignable_template(self, template_id: int) -> BillingPlanTemplate:
        template = self.session.get(BillingPlanTemplate, template_id)
        if template is None:
            raise ValueError(f"Unknown billing_plan_template id={template_id}")
        if not template.is_active:
            raise TemplateNotAssignableError(
                f"Template id={template_id} is deprecated (is_active=false) "
                "and cannot be assigned to new accounts.",
            )
        return template

    def _insert_active_assignment(
        self,
        *,
        billing_account_id: int,
        template: BillingPlanTemplate,
        created_by_user_id: Optional[str],
        change_reason: Optional[str],
        started_at: Optional[datetime],
    ) -> BillingPlanAssignment:
        assignment = BillingPlanAssignment(
            billing_account_id=billing_account_id,
            template_id=template.id,
            created_by_user_id=created_by_user_id,
            change_reason=change_reason,
            started_at=started_at,
        )
        self.session.add(assignment)
        self.session.flush()
        # Sync the denormalised pointer on BillingAccount.
        self.session.execute(
            update(BillingAccount)
            .where(BillingAccount.id == billing_account_id)
            .values(plan_assignment_id=assignment.id),
        )
        # Also update the in-memory ORM instance if it's already in this
        # session's identity map. The Core ``update`` above writes to the
        # DB row but does NOT refresh attributes on a loaded instance —
        # under ``expire_on_commit=False`` (which we use in production
        # and tests) that stale attribute would make subsequent
        # ``BillingAccountDAO.resolve_billing_mode`` lookups return CREDITS even though
        # the account is now on a METERED plan, until the instance is
        # explicitly refreshed.
        ba = self.session.get(BillingAccount, billing_account_id)
        if ba is not None:
            ba.plan_assignment_id = assignment.id
        self.session.flush()
        return assignment


# ---------------------------------------------------------------------------
# Pure helpers — calendar math + override precedence
#
# These live as module-level functions (not DAO methods) because they
# don't touch the session: they're shape rules over the data, and live
# next to the DAO so callers find them in one place.
# ---------------------------------------------------------------------------


def next_month_boundary_utc(reference: Optional[datetime] = None) -> datetime:
    """Return midnight UTC on the 1st of the month after ``reference``.

    Default reference is ``now()``. Used as the default ``effective_at``
    by ``set_plan`` so the prior plan's period completes cleanly before
    the new plan takes over.
    """
    moment = reference if reference is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.month == 12:
        return datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)


def is_month_boundary_utc(moment: datetime) -> bool:
    """True iff ``moment`` is exactly midnight UTC on the 1st of a month.

    Required by the AT_BOUNDARY proration policy: ``effective_at`` for
    plan changes/cancellations must land on a clean monthly boundary
    so the metered invoicer's per-period aggregation isn't split.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    return (
        moment_utc.day == 1
        and moment_utc.hour == 0
        and moment_utc.minute == 0
        and moment_utc.second == 0
        and moment_utc.microsecond == 0
    )


def _ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a naive timestamp to UTC; pass through ``None`` and aware values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


