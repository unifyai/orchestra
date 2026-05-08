"""DAO for ``BillingPlanTemplate``.

Templates are *immutable named billing configurations*. The DAO supports
create + read + catalog listing + lifecycle transitions on ``is_active``
only — there is intentionally no general update method. Mutating a template
would break audit truth (an invoice produced under v1 of "Vantage Q2" would
silently change). To "edit" a template, create a new one with
``supersedes_template_id`` pointing at the old one.

Catalog placement is described by two orthogonal booleans:

* ``is_custom``  — false = catalog (assignable to anyone); true = bespoke
                   per-customer contract (hidden from public catalog).
* ``is_active``  — true = accepts new assignments; false = deprecated
                   (existing assignments stay live, new ones blocked).

Plan-type ("PAYG" vs "COMMITMENT") is *derived* from ``commit_amount``
(NULL/zero ⇒ PAYG, positive ⇒ COMMITMENT) — there is no separate enum.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    DEFAULT_TEMPLATE_ID,
    BillingMode,
    BillingPlanAssignment,
    BillingPlanTemplate,
    CollectionMethod,
    CommitSchedule,
    FxPolicy,
    ProrationPolicy,
)


class TemplateInUseError(Exception):
    """Raised when ``deprecate`` is called on a template that still has
    one or more active ``BillingPlanAssignment`` rows pointing at it.

    Deprecation is meant to remove a plan from the assignable catalog
    *without* affecting customers already on it; allowing it to fire
    while accounts are still active would create a gap where the
    customer's current plan is silently no longer in the catalog and
    can't be re-assigned, e.g. after a cancel-then-reactivate flow. The
    operator must move every account off the template first (assign
    the successor / default plan), then deprecate.
    """

    def __init__(self, template_id: int, account_count: int) -> None:
        self.template_id = template_id
        self.account_count = account_count
        super().__init__(
            f"Cannot deprecate template id={template_id}: "
            f"{account_count} account(s) still have it as their active "
            "assignment. Move every account off the template (admin "
            "Set Plan) before deprecating.",
        )


class BillingPlanTemplateDAO:
    """Read/create/lifecycle access for ``billing_plan_template``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_template(  # noqa: WPS211 — many independent fields
        self,
        *,
        name: str,
        billing_mode: BillingMode,
        is_custom: bool = False,
        is_active: bool = True,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        commit_amount: Optional[Decimal] = None,
        currency: str = "USD",
        commit_period: Optional[str] = None,
        commit_schedule: Optional[str] = None,
        base_pricing_factor: Decimal = Decimal("1.0"),
        overage_pricing_factor: Decimal = Decimal("1.0"),
        collection_method: CollectionMethod = CollectionMethod.AUTO_CARD,
        proration_policy: ProrationPolicy = ProrationPolicy.PRORATE,
        credits_rollover_policy: Optional[str] = None,
        fx_policy: Optional[FxPolicy] = None,
        fx_locked_rate: Optional[Decimal] = None,
        supersedes_template_id: Optional[int] = None,
        created_by_user_id: Optional[str] = None,
    ) -> BillingPlanTemplate:
        """Create a new immutable template row.

        DB check constraints enforce the cross-field invariants
        (commit_amount > 0 requires a commit_period; credits_rollover_policy
        is COMMITMENT+CREDITS only; fx_policy is required for non-USD
        currencies and forbidden for USD; fx_locked_rate is required iff
        fx_policy=LOCKED_RATE).

        We do a tiny amount of pre-validation so the caller gets a
        ``ValueError`` instead of an opaque ``IntegrityError`` from the
        check constraints when the inputs are obviously inconsistent.
        """
        currency_upper = currency.upper()
        is_usd = currency_upper == "USD"

        if is_usd and fx_policy is not None:
            raise ValueError(
                "USD templates must have fx_policy=None; got "
                f"fx_policy={fx_policy.value}.",
            )
        if not is_usd and fx_policy is None:
            raise ValueError(
                f"Non-USD templates (currency={currency_upper}) require an "
                "fx_policy (LOCKED_RATE / SPOT / PERIOD_AVERAGE).",
            )

        if fx_policy == FxPolicy.LOCKED_RATE and (
            fx_locked_rate is None or fx_locked_rate <= 0
        ):
            raise ValueError(
                "fx_policy=LOCKED_RATE requires a positive fx_locked_rate.",
            )
        if fx_policy != FxPolicy.LOCKED_RATE and fx_locked_rate is not None:
            raise ValueError(
                "fx_locked_rate is only allowed when fx_policy=LOCKED_RATE; "
                f"got fx_policy={fx_policy.value if fx_policy else None}.",
            )

        # UPFRONT-specific guard rails (mirror the DB CHECK constraints
        # so the caller gets a clear ValueError instead of an opaque
        # IntegrityError).
        if commit_schedule == CommitSchedule.UPFRONT.value:
            if proration_policy != ProrationPolicy.FULL_FIRST:
                raise ValueError(
                    "commit_schedule=UPFRONT requires "
                    "proration_policy=FULL_FIRST (the full commit is "
                    "billed on the contract anniversary; prorating it "
                    "across a mid-month start is ambiguous). Got "
                    f"proration_policy={proration_policy.value}.",
                )
            if fx_policy == FxPolicy.PERIOD_AVERAGE:
                raise ValueError(
                    "commit_schedule=UPFRONT is incompatible with "
                    "fx_policy=PERIOD_AVERAGE (the 'average over the "
                    "billing period' concept is ambiguous when the "
                    "period spans multiple invoiced months). Use "
                    "LOCKED_RATE or SPOT instead.",
                )

        template = BillingPlanTemplate(
            name=name,
            display_name=display_name,
            description=description,
            billing_mode=billing_mode.value,
            commit_amount=commit_amount,
            currency=currency_upper,
            commit_period=commit_period,
            commit_schedule=commit_schedule,
            base_pricing_factor=base_pricing_factor,
            overage_pricing_factor=overage_pricing_factor,
            collection_method=collection_method.value,
            proration_policy=proration_policy.value,
            credits_rollover_policy=credits_rollover_policy,
            fx_policy=fx_policy.value if fx_policy is not None else None,
            fx_locked_rate=fx_locked_rate,
            is_custom=is_custom,
            is_active=is_active,
            supersedes_template_id=supersedes_template_id,
            created_by_user_id=created_by_user_id,
        )
        self.session.add(template)
        self.session.flush()
        return template

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_by_id(self, template_id: int) -> Optional[BillingPlanTemplate]:
        return self.session.get(BillingPlanTemplate, template_id)

    def get_by_name(self, name: str) -> Optional[BillingPlanTemplate]:
        return (
            self.session.execute(
                select(BillingPlanTemplate).where(BillingPlanTemplate.name == name),
            )
            .scalars()
            .first()
        )

    def get_default(self) -> BillingPlanTemplate:
        """Return the seeded default template (id = DEFAULT_TEMPLATE_ID).

        Raises if the seed row is missing — that would indicate a corrupted
        migration state and any caller would otherwise misbehave silently.
        """
        template = self.get_by_id(DEFAULT_TEMPLATE_ID)
        if template is None:
            raise RuntimeError(
                f"default template (id={DEFAULT_TEMPLATE_ID}) not found. "
                "Migration 'metered_and_billing_plan' must run before any billing ops.",
            )
        return template

    def list_catalog(
        self,
        *,
        include_custom: Optional[bool] = None,
        include_inactive: bool = False,
    ) -> List[BillingPlanTemplate]:
        """List templates filtered by catalog placement.

        Defaults match what a customer-facing pricing page should see:
        catalog-only (``is_custom=false``) and active-only
        (``is_active=true``).

        * ``include_custom=None``  — both custom and non-custom rows
                                     (admin tooling).
        * ``include_custom=True``  — only custom rows.
        * ``include_custom=False`` — only non-custom (catalog) rows.
        * ``include_inactive=True`` — also return deprecated rows.
        """
        clauses = []
        if include_custom is True:
            clauses.append(BillingPlanTemplate.is_custom.is_(True))
        elif include_custom is False:
            clauses.append(BillingPlanTemplate.is_custom.is_(False))
        if not include_inactive:
            clauses.append(BillingPlanTemplate.is_active.is_(True))

        query = select(BillingPlanTemplate).order_by(BillingPlanTemplate.id)
        if clauses:
            query = query.where(and_(*clauses))
        return list(self.session.execute(query).scalars().all())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def count_active_assignments(self, template_id: int) -> int:
        """Count BillingPlanAssignment rows on this template that are still
        live (``ended_at IS NULL``).

        The unique partial index on
        ``billing_plan_assignment(billing_account_id) WHERE ended_at IS
        NULL`` already enforces "at most one active row per account" so
        this is equivalent to "number of accounts currently on the
        template" without an extra DISTINCT.
        """
        from sqlalchemy import func as _func

        return int(
            self.session.execute(
                select(_func.count())
                .select_from(BillingPlanAssignment)
                .where(
                    BillingPlanAssignment.template_id == template_id,
                    BillingPlanAssignment.ended_at.is_(None),
                ),
            ).scalar_one(),
        )

    def deprecate(self, template_id: int) -> None:
        """Mark an active template as deprecated (no new assignments).

        Refuses with :class:`TemplateInUseError` if any account still has
        an active ``BillingPlanAssignment`` pointing at this template —
        deprecating mid-billing would leave the customer on a plan
        that's no longer in the catalog (a state the rest of the system
        treats as drift). The operator must move every account off the
        template first (admin Set Plan) before retiring it.

        Existing assignments keep working *after* deprecation only when
        the template was already empty — only new assignments are then
        blocked at the DAO layer (see
        :meth:`BillingPlanAssignmentDAO.set_plan`). The template row
        itself is preserved for audit, including the ``is_custom``
        flag — a deprecated bespoke contract is still recognisable as
        bespoke.

        No-op for templates that are already inactive.
        """
        # Guard FIRST, before the UPDATE: we want a clean refusal, not
        # a state where ``is_active`` flips to false alongside the
        # raised exception (raising would roll back, but doing the
        # cheap check up-front makes the intent obvious to readers).
        active_count = self.count_active_assignments(template_id)
        if active_count > 0:
            raise TemplateInUseError(template_id, active_count)
        self.session.execute(
            update(BillingPlanTemplate)
            .where(
                BillingPlanTemplate.id == template_id,
                BillingPlanTemplate.is_active.is_(True),
            )
            .values(is_active=False),
        )

    def reactivate(self, template_id: int) -> None:
        """Reverse a previous :meth:`deprecate` (rare; provided for symmetry).

        Safe to call on already-active templates (no-op).
        """
        self.session.execute(
            update(BillingPlanTemplate)
            .where(BillingPlanTemplate.id == template_id)
            .values(is_active=True),
        )


__all__ = ["BillingPlanTemplateDAO", "TemplateInUseError"]
