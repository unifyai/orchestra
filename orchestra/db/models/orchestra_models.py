import uuid
from datetime import datetime
from enum import Enum  # noqa: F401  — re-exported below

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import backref, relationship, validates

from orchestra.db.base import Base


def _new_string_uuid() -> str:
    return str(uuid.uuid4())


# Kernel models live in orchestra. Re-exported here so platform
# code can keep `from orchestra.db.models.orchestra_models import Project`
# and treat this file as the single platform-facing model surface.
from orchestra.db.models.core_models import (  # noqa: E402, F401
    ActiveDerivedLog,
    Context,
    ContextCounter,
    ContextVersion,
    Embedding,
    EmbeddingQueue,
    FieldType,
    LogEvent,
    LogEventContext,
    LogEventVersion,
    LogUniqueConstraint,
    Project,
    ProjectVersion,
)

# Billing-domain enums + sentinel constants live in their own module so
# non-ORM consumers (lib, routines, web/api) can import them without
# pulling the full 4k-line model file.
from orchestra.db.models.enums import (  # noqa: E402, F401
    DEFAULT_PLAN_GROUP_ID,
    DEFAULT_TEMPLATE_ID,
    RECHARGE_TYPE_AUTO,
    RECHARGE_TYPE_MONTHLY_COMMIT,
    RECHARGE_TYPE_OVERAGE_TRUEUP,
    RECHARGE_TYPE_PAYMENT,
    RECHARGE_TYPE_PROMO,
    RECHARGE_TYPE_PRORATION,
    BillingMode,
    CollectionMethod,
    CommitPeriod,
    CommitSchedule,
    CreditsRolloverPolicy,
    FxPolicy,
    PaymentMethodType,
    ProrationPolicy,
    RechargeStatus,
    StrEnum,
)

# ContactMembership has a `relationship` column, so keep a callable alias for
# relationship() inside that class body.
orm_relationship = relationship


class BillingAccount(Base):
    """
    Shared billing entity for User and Organization.

    Consolidates all billing-related fields (credits, Stripe customer,
    account status) AND optional business profile fields (tax ID, address, business name)
    into a single table. Both User and Organization link here via FK.

    This eliminates field duplication and provides a single code path for all billing logic.
    """

    __tablename__ = "billing_account"

    id = Column(Integer, primary_key=True)

    # === CORE BILLING ===
    credits = Column(Numeric, nullable=False, default=0, server_default="0")
    stripe_customer_id = Column(String, nullable=True, unique=True, index=True)
    # === SELF-SERVE SUBSCRIPTION (CREDITS tier plans) ===
    # The active Stripe Subscription backing a self-serve CREDITS account on
    # one of the seeded tier templates (collection_method=STRIPE_SUBSCRIPTION).
    # NULL for accounts that have never subscribed (free/unsubscribed state on
    # the default template) and for METERED enterprise accounts (which invoice
    # in arrears via ``monthly_metered_invoicer`` rather than a subscription).
    # The subscription is the collection engine; the plan template stays the
    # source of truth for the monthly credit grant.
    stripe_subscription_id = Column(String, nullable=True, index=True)
    # End of the current Stripe subscription period (next renewal / credit
    # reset). Mirrored from Stripe ``current_period_end`` on each
    # ``invoice.paid`` and ``customer.subscription.updated`` webhook so the
    # console can render the next-renewal date without a Stripe round-trip.
    # NULL for unsubscribed/free accounts and METERED enterprise accounts.
    current_period_end = Column(TIMESTAMP(timezone=True), nullable=True)
    # Whether the active subscription is scheduled to cancel at the end of the
    # current period (Stripe ``cancel_at_period_end``). Set immediately on an
    # in-app cancel and kept in sync from the ``customer.subscription.updated``
    # webhook (so a Stripe-Dashboard/Portal cancel reflects too), and reset to
    # False on (re)subscribe and on final cancellation. Lets the console render
    # a persistent "cancels on X" indicator instead of only a transient toast.
    subscription_cancel_at_period_end = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # Opt-in: when the wallet depletes (balance <= 0) auto-upgrade the
    # subscription to the next tier up the ladder (capped at the top tier;
    # never auto-downgrades). False = hard stop at depletion (manual upgrade).
    auto_increment = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # Per-period high-water-mark of plan credits already granted this cycle
    # (in credits). Reset to the tier's grant on each paid cycle
    # (``invoice.paid``); on a mid-cycle upgrade we only grant the amount
    # *above* this mark, so repeatedly toggling upgrade/downgrade cannot mint
    # free credits (a downgrade never lowers the mark and never claws back).
    plan_credits_granted_period = Column(
        Numeric,
        nullable=False,
        default=0,
        server_default="0",
    )
    # Idempotency stamp for the pre-expiry credit reminder
    # (``orchestra.routines.credit_expiry_reminder``): the ``expires_at`` of
    # the soonest grant we last emailed a "use-it-or-lose-it" reminder for.
    # The daily routine skips an account whose soonest upcoming expiry still
    # matches this value, so each distinct expiry triggers at most one email.
    credit_expiry_reminded_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    account_status = Column(
        String,
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
    )  # ACTIVE, SUSPENDED, CLOSED
    suspension_reason = Column(
        String,
        nullable=True,
        default=None,
    )  # dispute, admin_freeze, past_due — NULL when ACTIVE
    # Delinquency marker for the *soft* dunning window: set to the moment of
    # the first failed subscription payment and kept while Stripe retries
    # (account stays ACTIVE — service is not cut off). Cleared the moment
    # payment recovers; a fully-exhausted dunning cycle escalates to a hard
    # suspension (``account_status='SUSPENDED'``, ``suspension_reason='past_due'``)
    # instead. Kept separate from ``suspension_reason`` so that field keeps its
    # "why is this account suspended" meaning (NULL while ACTIVE).
    payment_past_due_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        default=None,
    )
    billing_setup_complete = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # === MANAGED BILLING v2 — current plan assignment ===
    # Points to the currently-active ``BillingPlanAssignment`` row (which in
    # turn references a ``BillingPlanTemplate``). Every account carries a
    # real pointer at all times — pristine self-serve accounts get a
    # default plan assignment at signup via ``BillingAccountDAO.create``,
    # the migration backfilled the same for accounts that pre-date v2, and
    # ``set_plan`` always closes-and-inserts (including for cancellations,
    # which insert a fresh default plan row). The column is *nullable in
    # the DB* (PostgreSQL ``NOT NULL`` is not deferrable, which would
    # create a chicken-and-egg with the assignment row's FK back to the
    # BA) but **NOT NULL by application contract** — any NULL pointer in
    # production is corruption that the daily reconciliation routine flags
    # as ``plan_assignment_null_pointer`` (critical). Resolve via
    # ``BillingAccountDAO.resolve_billing_mode()`` /
    # ``BillingPlanAssignmentDAO.resolve_effective_plan()``.
    plan_assignment_id = Column(
        BigInteger,
        ForeignKey("billing_plan_assignment.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # === BILLING PROFILE ===
    # The editable billing profile (name, email, address, tax ID) is NOT
    # stored here — Stripe is the single source of truth. We persist only
    # two non-PII *derived flags* the hot/batch paths need without a live
    # Stripe call:
    #
    #   * ``is_business`` — drives business-vs-personal recurring price and
    #     tax treatment. Set when a tax ID is saved and refined by the
    #     ``customer.tax_id.*`` webhook (flipped off if Stripe reports the
    #     ID ``unverified``). Resolve via ``resolve_is_business``.
    #   * ``billing_setup_complete`` — true once a complete, tax-resolvable
    #     address has been synced to Stripe; gates self-serve subscribe.
    #
    # Everything identifying (name / billing_email / billing_address /
    # tax_id / tax_id_type / verification status) lives only on the Stripe
    # Customer and is fetched on demand (see
    # ``fetch_billing_profile_from_stripe``). This removes the two-way
    # reconciliation the webhooks used to perform.
    is_business = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    # Per-customer override for the payment methods exposed on
    # ``send_invoice`` Stripe invoices. NULL means "use the invoicer's
    # defaults for this template's collection_method"
    # (``['card']`` for AUTO_CARD, ``['card', 'customer_balance']``
    # for SEND_INVOICE_NET_30). A non-NULL list lets ops mark a
    # customer as "wire only" (``['customer_balance']``) or
    # "card only" without minting a new BillingPlanTemplate version.
    # Validated against the supported set in
    # ``BillingAccountDAO.set_payment_preferences``.
    preferred_payment_method_types = Column(
        ARRAY(String),
        nullable=True,
    )

    # === PLAN GROUP — self-serve switch catalog ===
    # Optional pointer at the ``PlanGroup`` this account is allowed to
    # self-serve switch within. NULL means "no self-serve switching for
    # this account" (admins can still call ``set_plan`` directly via the
    # admin API). When set, the customer billing page surfaces a
    # "Switch plan" section listing every active member of this group;
    # the customer endpoint refuses to assign a template that isn't a
    # member, so this column is the ACL boundary.
    #
    # Deliberately a single FK (not a junction table) — a billing
    # account is on at most one self-serve catalog at a time, mirroring
    # the single-pointer pattern used for ``plan_assignment_id``.
    # Members of the group can include the currently-active template
    # but don't have to (admins may have moved an account onto a custom
    # template that's outside its public ladder).
    #
    # Defaults to ``DEFAULT_PLAN_GROUP_ID = 1`` (the system default
    # group) at INSERT time on both the Python and DB sides. The
    # column is ``NOT NULL`` by schema invariant — every account is
    # always on *some* group, mirroring the ``plan_assignment_id``
    # contract. There is intentionally no "opt-out" state: customers
    # whose current template isn't in their assigned group simply
    # see no switcher (the customer-facing endpoint hides the section
    # when no member is ``is_current``).
    #
    # ``ON DELETE RESTRICT`` on the FK enforces the same invariant
    # at the DB layer — deleting a group with referencing accounts
    # would otherwise silently NULL the column.
    plan_group_id = Column(
        BigInteger,
        ForeignKey("plan_group.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        default=DEFAULT_PLAN_GROUP_ID,
        server_default=text(str(DEFAULT_PLAN_GROUP_ID)),
    )

    # === TIMESTAMPS ===
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # === RELATIONSHIPS ===
    recharges = relationship(
        "Recharge",
        back_populates="billing_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    __table_args__ = (
        sa.CheckConstraint(
            "account_status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')",
            name="ck_billing_account_status",
        ),
    )


class Recharge(Base):
    """Model class for the recharge table."""

    __tablename__ = "recharge"

    id = Column(Integer(), primary_key=True)
    at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        default=datetime.utcnow,
    )
    # Billing account that this recharge belongs to
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    quantity = Column(Numeric(), nullable=False)
    amount_usd = Column(Numeric(), nullable=False)
    type = Column(String())
    transaction_id = Column(String())
    status = Column(
        String(),
        nullable=False,
        server_default=RechargeStatus.PENDING_INVOICE.value,
    )
    stripe_invoice_id = Column(String)
    invoice_group = Column(Date)
    # Managed billing: link to the BillingPlanAssignment that produced
    # this recharge. NULL for legacy rows and for all CREDITS-mode auto-recharge
    # / payment / promo rows where no plan applies. Set for METERED-mode rows
    # produced by ``monthly_metered_invoicer``.
    plan_id = Column(
        BigInteger,
        ForeignKey("billing_plan_assignment.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Optional audit JSONB. For METERED recharges produced by the metered
    # invoicer, captures: ``raw_usage_usd``, ``base_pricing_factor``,
    # ``overage_pricing_factor``, ``contract_usage_local``, ``commit_amount``,
    # ``overage_local``, ``fx_rate``, ``period_start``, ``period_end``. Lets
    # a customer dispute recompute the invoice from first principles without
    # relying on transient state.
    detail = Column(JSONB, nullable=True)

    # ORM relationships
    billing_account = relationship("BillingAccount", back_populates="recharges")

    __table_args__ = (
        Index("idx_recharge_pending", "status", "invoice_group"),
        sa.CheckConstraint(
            "status IN ('PENDING_INVOICE','PAID','FAILED','INVOICE_CREATED','DISPUTED')",
            name="ck_recharge_status",
        ),
    )


class BillingPlanTemplate(Base):
    """An immutable, named billing configuration.

    Many ``BillingAccount``s can point at the same template via
    ``BillingPlanAssignment``. Templates are *never mutated* after creation —
    to change terms, create a new row (optionally chained via
    ``supersedes_template_id``) and re-assign. This preserves audit truth:
    a customer disputing an invoice can always reconstruct the exact terms
    in force at the time.

    The fields below are grouped:

    * **Identity**         — ``name``, ``display_name``, ``description``
    * **Settlement**       — ``billing_mode``  (CREDITS vs METERED)
    * **Commit shape**     — ``commit_amount``, ``commit_period``,
                             ``commit_schedule``  (all NULL ⇒ PAYG)
    * **Pricing**          — ``base_pricing_factor``, ``overage_pricing_factor``
    * **Currency & FX**    — ``currency``, ``fx_policy``, ``fx_locked_rate``
    * **Collection**       — ``collection_method``
    * **Lifecycle**        — ``proration_policy``, ``credits_rollover_policy``
    * **Catalog**          — ``is_custom``, ``is_active``,
                             ``supersedes_template_id``
    * **Audit**            — ``created_at``, ``created_by_user_id``

    Plan shape ("PAYG" vs "COMMITMENT") is derived, not stored:
    ``commit_amount`` IS NULL / 0 → PAYG, positive → COMMITMENT.

    Catalog placement uses two orthogonal booleans rather than a single
    enum: ``is_custom`` toggles bespoke-vs-catalog, ``is_active`` toggles
    live-vs-deprecated. A deprecated row keeps its ``is_custom`` flag so
    audit history stays honest.

    The implicit platform default lives at ``DEFAULT_TEMPLATE_ID = 1``
    (seeded by the migration). Every account is assigned to *some*
    template — there is no "pristine, no assignment" state.
    """

    __tablename__ = "billing_plan_template"

    # ── Identity ─────────────────────────────────────────────────────────
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    #: Internal identifier (kebab-case, unique). Operators-only.
    name = Column(String(120), nullable=False, unique=True)
    #: Customer-facing label (invoices, dashboards). Falls back to ``name``.
    display_name = Column(String(120), nullable=True)
    description = Column(Text, nullable=True)

    # ── Settlement ──────────────────────────────────────────────────────
    #: CREDITS = prepaid wallet w/ auto-recharge.
    #: METERED = invoice in arrears at month-end.
    billing_mode = Column(
        String,
        nullable=False,
        server_default=BillingMode.CREDITS.value,
    )

    # ── Commit shape ────────────────────────────────────────────────────
    # Three NULLs ⇒ Pay-As-You-Go. Positive ``commit_amount`` requires
    # both ``commit_period`` and ``commit_schedule`` (CHECK enforced).
    #: Monthly minimum in the invoice currency, or NULL/0 for PAYG.
    commit_amount = Column(Numeric, nullable=True)
    #: How often the commit resets (MONTHLY / QUARTERLY / ANNUAL).
    commit_period = Column(String, nullable=True)
    #: When the commit is invoiced relative to its period (AMORTISED / UPFRONT).
    commit_schedule = Column(String, nullable=True)

    # ── Pricing ─────────────────────────────────────────────────────────
    # No overage_policy / monthly cap — the platform never blocks usage
    # based on plan terms; usage above commit always invoices at
    # ``base × overage`` rate. Operators can void specific Stripe
    # invoices when a charge shouldn't stand.
    #
    # Two-rate split lets enterprise contracts express the standard
    # "discount within commit, premium above commit" structure that
    # AWS Reserved Instances, Snowflake, Databricks etc. all use. The
    # rates **stack** on overage: ``base_pricing_factor`` applies to
    # all usage uniformly (commit-included + overage + PAYG), and
    # ``overage_pricing_factor`` is an additional uplift on top, only
    # for the overage portion. PAYG plans only ever exercise
    # ``base_pricing_factor``; ``overage_pricing_factor`` is irrelevant
    # for them and conventionally left at 1.0.
    #: Multiplier on raw USD usage for ALL usage (commit-included,
    #: overage, and PAYG). 1.00 = list price; 0.80 = 20% discount;
    #: 1.10 = 10% premium; etc. Combined with ``overage_pricing_factor``
    #: above commit (effective overage rate = base × overage).
    base_pricing_factor = Column(
        Numeric,
        nullable=False,
        server_default="1.0",
    )
    #: ADDITIONAL multiplier stacked on top of ``base_pricing_factor``
    #: for usage ABOVE commit only. Only meaningful for COMMITMENT
    #: plans; defaults to 1.00 (no overage penalty — base discount /
    #: markup continues to apply uniformly above commit). Set > 1.00
    #: to charge a premium for over-consumption (e.g. 1.25 = "25%
    #: uplift over the base rate above commit"); a customer on a
    #: ``base=0.80, overage=1.25`` plan pays ``0.80×1.25 = 1.00`` of
    #: list price for above-commit usage, vs. ``0.80`` within commit.
    overage_pricing_factor = Column(
        Numeric,
        nullable=False,
        server_default="1.0",
    )

    # ── Currency & FX ───────────────────────────────────────────────────
    # USD templates run with ``fx_policy IS NULL``; non-USD templates
    # MUST set one (CHECK enforced). The metered invoicer dispatches on
    # ``fx_policy`` to fetch / pin the conversion rate.
    #: ISO-4217 invoice currency (USD / GBP / EUR / …).
    currency = Column(String(3), nullable=False, server_default="USD")
    #: NULL for USD; LOCKED_RATE / SPOT / PERIOD_AVERAGE for non-USD.
    fx_policy = Column(String(32), nullable=True)
    #: Required iff ``fx_policy = LOCKED_RATE``. 8-frac digits cover all G10/EM crosses.
    fx_locked_rate = Column(Numeric(18, 8), nullable=True)

    # ── Collection ──────────────────────────────────────────────────────
    #: AUTO_CARD = Stripe pulls from saved card.
    #: SEND_INVOICE_NET_30 = customer pushes (wire / customer balance / …).
    collection_method = Column(
        String,
        nullable=False,
        server_default=CollectionMethod.AUTO_CARD.value,
    )

    # ── Lifecycle ───────────────────────────────────────────────────────
    #: How partial first/last periods are billed.
    proration_policy = Column(
        String,
        nullable=False,
        server_default=ProrationPolicy.PRORATE.value,
    )
    #: COMMITMENT+CREDITS only — what happens to unused credits at
    #: period-end. NULL for every other quadrant (CHECK enforced).
    credits_rollover_policy = Column(String, nullable=True)

    # ── Catalog placement ───────────────────────────────────────────────
    #: false = catalog (assignable to anyone). true = bespoke per-customer.
    is_custom = Column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    #: false = deprecated (no new assignments; existing ones keep billing).
    is_active = Column(
        Boolean,
        nullable=False,
        server_default="true",
    )
    #: Optional pointer at the template this one replaces (audit chain).
    supersedes_template_id = Column(
        BigInteger,
        ForeignKey("billing_plan_template.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Audit ───────────────────────────────────────────────────────────
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "billing_mode IN ('CREDITS', 'METERED')",
            name="ck_plan_template_billing_mode",
        ),
        sa.CheckConstraint(
            "commit_period IS NULL OR commit_period IN "
            "('MONTHLY', 'QUARTERLY', 'ANNUAL')",
            name="ck_plan_template_commit_period",
        ),
        sa.CheckConstraint(
            "commit_schedule IS NULL OR commit_schedule IN " "('AMORTISED', 'UPFRONT')",
            name="ck_plan_template_commit_schedule",
        ),
        sa.CheckConstraint(
            "collection_method IN "
            "('AUTO_CARD', 'SEND_INVOICE_NET_30', 'STRIPE_SUBSCRIPTION')",
            name="ck_plan_template_collection_method",
        ),
        sa.CheckConstraint(
            "proration_policy IN ('PRORATE', 'SKIP_FIRST', 'FULL_FIRST')",
            name="ck_plan_template_proration_policy",
        ),
        sa.CheckConstraint(
            "credits_rollover_policy IS NULL OR credits_rollover_policy IN "
            "('ROLL_OVER', 'FORFEIT_AT_PERIOD_END')",
            name="ck_plan_template_credits_rollover_policy",
        ),
        # Pricing factors must be strictly positive (zero would silently
        # waive every charge — never what an operator wants; if a plan
        # truly shouldn't bill it should be marked inactive instead).
        sa.CheckConstraint(
            "base_pricing_factor > 0 AND overage_pricing_factor > 0",
            name="ck_plan_template_pricing_factors_positive",
        ),
        # Commit + period travel together: a positive commit amount
        # requires a period to attach to.
        sa.CheckConstraint(
            "(commit_amount IS NULL OR commit_amount = 0) OR "
            "commit_period IS NOT NULL",
            name="ck_plan_template_commit_has_period",
        ),
        # UPFRONT-schedule plans bill the full ``commit_amount`` on
        # contract anniversaries; prorating that lump sum across a
        # mid-month start would be confusing for both customer and
        # accounting (the customer is buying a full period of
        # coverage, not a fraction). Require FULL_FIRST proration so
        # the first invoice carries the full commit and the contract
        # anniversaries land on round month boundaries thereafter.
        sa.CheckConstraint(
            "commit_schedule IS DISTINCT FROM 'UPFRONT' OR "
            "proration_policy = 'FULL_FIRST'",
            name="ck_plan_template_upfront_requires_full_first",
        ),
        # PERIOD_AVERAGE FX averages business-day rates across the
        # billing period; for UPFRONT-schedule plans the "billing
        # period" is the calendar month the anniversary lands in, but
        # the customer thinks of FX as locked at signing. Mixing the
        # two is ambiguous (which month's average for a 12-month
        # contract that anniversaries in March?). Require LOCKED_RATE
        # or SPOT for UPFRONT non-USD plans.
        sa.CheckConstraint(
            "commit_schedule IS DISTINCT FROM 'UPFRONT' OR "
            "fx_policy IS NULL OR fx_policy IN ('LOCKED_RATE', 'SPOT')",
            name="ck_plan_template_upfront_no_period_average_fx",
        ),
        # ``credits_rollover_policy`` is only meaningful in the
        # COMMITMENT + CREDITS cell. Anything else must leave it NULL.
        sa.CheckConstraint(
            "credits_rollover_policy IS NULL OR "
            "(commit_amount IS NOT NULL AND commit_amount > 0 "
            "AND billing_mode = 'CREDITS')",
            name="ck_plan_template_credits_rollover_scope",
        ),
        # FX policy invariants — kept in sync with the migration so
        # ``Base.metadata.create_all`` (used by some test paths) carries
        # the same guarantees as alembic-managed prod databases.
        sa.CheckConstraint(
            "fx_policy IS NULL OR "
            "fx_policy IN ('LOCKED_RATE', 'SPOT', 'PERIOD_AVERAGE')",
            name="ck_plan_template_fx_policy",
        ),
        # USD ⇔ no-FX, non-USD ⇔ FX policy required. A single check
        # encodes both directions.
        sa.CheckConstraint(
            "(currency = 'USD' AND fx_policy IS NULL) OR "
            "(currency <> 'USD' AND fx_policy IS NOT NULL)",
            name="ck_plan_template_fx_required_for_non_usd",
        ),
        sa.CheckConstraint(
            "(fx_policy = 'LOCKED_RATE' AND fx_locked_rate IS NOT NULL "
            "AND fx_locked_rate > 0) OR "
            "(fx_policy IS DISTINCT FROM 'LOCKED_RATE' "
            "AND fx_locked_rate IS NULL)",
            name="ck_plan_template_fx_locked_rate",
        ),
        Index("ix_plan_template_is_active", "is_active"),
    )


class BillingPlanAssignment(Base):
    """Time-bounded assignment of a ``BillingPlanTemplate`` to a ``BillingAccount``.

    Each row represents one phase of one account's plan history. Plan
    changes create a new row and set ``ended_at`` on the previous row.
    Cancelling a non-default plan creates an explicit default plan row
    (so every state is described by a row). **Every account has exactly
    one active assignment at all times** — pristine self-serve accounts
    get a default plan assignment at signup
    (``BillingAccountDAO.create``), and the migration backfilled one
    for every pre-v2 account. There is no "implicit default" /
    "no row" shape; ``BillingAccount.plan_assignment_id IS NULL`` is
    schema corruption that the daily reconciliation routine flags as
    critical (the column is nullable in the DB only because
    ``NOT NULL`` is not deferrable, which would create a
    chicken-and-egg at row-creation time).

    A unique partial index ensures at most one currently-active
    assignment per billing account. Per-assignment overrides are
    supported sparingly for negotiated tweaks that don't justify a
    whole new template.

    History is reconstructed by ordering on ``started_at DESC`` filtered
    by ``billing_account_id`` — there is intentionally no per-row
    ``supersedes`` pointer (it would duplicate the time order without
    adding information).
    """

    __tablename__ = "billing_plan_assignment"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id = Column(
        BigInteger,
        ForeignKey("billing_plan_template.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Lifecycle window. ``ended_at`` IS NULL means currently active.
    started_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    ended_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Audit
    created_by_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    change_reason = Column(Text, nullable=True)

    # ORM relationships
    template = relationship("BillingPlanTemplate")

    __table_args__ = (
        # At most one currently-active assignment per billing account.
        Index(
            "ux_billing_plan_assignment_active_unique",
            "billing_account_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_billing_plan_assignment_account_started",
            "billing_account_id",
            "started_at",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_billing_plan_assignment_window",
        ),
    )


class PlanGroup(Base):
    """A curated bundle of ``BillingPlanTemplate``s that an account can switch between.

    Plan groups exist purely to scope the customer-facing self-serve
    switch endpoint: ``BillingAccount.plan_group_id`` points at one of
    these, and the customer is allowed to assign themselves any active
    template that is a member. Templates themselves are unchanged and
    a single template may belong to many groups (e.g. the public
    ClientGamma ladder + a bespoke custom-tier ladder for a specific
    customer).

    Membership semantics are carried by ``PlanGroupMember.position``:
    NULL = unordered alternatives (rendered as side-by-side cards),
    populated = ordered ladder rung (lower position = "smaller" tier,
    used to detect downgrades for AT_BOUNDARY-deferral). The schema
    allows mixing — but in practice a group either declares all
    positions or none.
    """

    __tablename__ = "plan_group"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # Operator-facing slug, unique catalog-wide. Exposed in admin
    # logs / URLs; customer-facing UI uses ``display_name``.
    name = Column(String, nullable=False, unique=True)
    display_name = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    members = relationship(
        "PlanGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PlanGroupMember.position.asc().nulls_last(), "
        "PlanGroupMember.added_at.asc()",
    )

    __table_args__ = (Index("ix_plan_group_is_active", "is_active"),)


class PlanGroupMember(Base):
    """Junction row: a ``BillingPlanTemplate`` is offered as part of a ``PlanGroup``.

    ``position`` carries the rung order: NULL means "this group is an
    unordered set" (UX renders as cards), an integer means "ladder
    rung at this position" — lower = "smaller" tier, so a target
    position lower than the current account's position constitutes a
    *downgrade* for the AT_BOUNDARY policy. Positions must be unique
    within a group when set (enforced by a partial unique index in the
    migration so unordered groups can have many NULLs).
    """

    __tablename__ = "plan_group_member"

    group_id = Column(
        BigInteger,
        ForeignKey("plan_group.id", ondelete="CASCADE"),
        primary_key=True,
    )
    template_id = Column(
        BigInteger,
        ForeignKey("billing_plan_template.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    # NULL = unordered offer; integer = ladder rung. Lower = smaller
    # tier (downgrade target). Must be unique within a group when set.
    position = Column(Integer, nullable=True)
    added_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    group = relationship("PlanGroup", back_populates="members")
    template = relationship("BillingPlanTemplate")

    __table_args__ = (
        sa.CheckConstraint(
            "position IS NULL OR position >= 0",
            name="ck_plan_group_member_position_non_negative",
        ),
        Index(
            "ux_plan_group_member_position",
            "group_id",
            "position",
            unique=True,
            postgresql_where=text("position IS NOT NULL"),
        ),
        Index("ix_plan_group_member_template_id", "template_id"),
    )


class WebhookLog(Base):
    """
    Model for tracking processed Stripe webhook events to enforce idempotency.
    Each record represents a successfully processed webhook event.
    """

    __tablename__ = "webhook_log"

    id = Column(String, primary_key=True)
    event_id = Column(String, unique=True, nullable=False)
    event_type = Column(String, nullable=False)
    processed_at = Column(TIMESTAMP, nullable=False, server_default=func.now())


class RechargeType(Base):
    """Model class for the recharge_type table."""

    __tablename__ = "recharge_type"

    type = Column(String(), primary_key=True)


class User(Base):
    """
    Consolidated user model.

    Previously split across `users` (billing) and `auth_user` (profile).
    Now a single table matching Organization, OrganizationMember, Team architecture.

    Billing fields live on BillingAccount (linked via billing_account_id FK).
    """

    __tablename__ = "user"

    # === IDENTITY FIELDS ===
    id = Column(String, primary_key=True, default=_new_string_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String)
    last_name = Column(String)
    job_title = Column(String)
    bio = Column(String, nullable=True)
    image = Column(String)
    timezone = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)
    whatsapp_number = Column(String, nullable=True)
    discord_id = Column(String, nullable=True)
    # Voice enrollment: gs:// URL of the user's recorded voice sample, used by
    # assistants to voice-verify the user's turns on calls.
    voice_sample = Column(String, nullable=True)
    voice_sample_uploaded_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === BILLING (via BillingAccount) ===
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # === ACCOUNT SETTINGS ===
    # Toggles managed by usage quotas
    queries_enabled = Column(Boolean, nullable=False, server_default="true")
    evaluations_enabled = Column(Boolean, nullable=False, server_default="true")
    personal_workspace_disabled_at = Column(TIMESTAMP(timezone=True), nullable=True)
    personal_workspace_disabled_reason = Column(Text, nullable=True)
    personal_workspace_disabled_org_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    store_prompts = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # === SPENDING LIMITS ===
    # Monthly spending limit for this user's assistants (NULL = no limit)
    # Cannot exceed the org's monthly_spending_cap if user is in an org
    monthly_spending_cap = Column(Numeric, nullable=True)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === TIMESTAMPS ===
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # === RELATIONSHIPS ===
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    interfaces = relationship(
        "Interface",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "uq_user_whatsapp_number",
            "whatsapp_number",
            unique=True,
            postgresql_where=text("whatsapp_number IS NOT NULL"),
        ),
        Index(
            "uq_user_discord_id",
            "discord_id",
            unique=True,
            postgresql_where=text("discord_id IS NOT NULL"),
        ),
    )


class UserPresence(Base):
    """Last-seen heartbeat for a user's Console session.

    A user is considered online when ``last_seen_at`` is within
    ``PRESENCE_ONLINE_THRESHOLD_SECONDS`` of now. Rows are upserted by the
    presence heartbeat endpoint; absence of a row means "never seen".
    """

    __tablename__ = "user_presence"

    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_seen_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DmThread(Base):
    """A direct human-to-human conversation inside one organization.

    The user pair is stored normalized (``user_a_id < user_b_id``
    lexicographically) so one row exists per pair per organization.
    """

    __tablename__ = "dm_thread"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_a_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_b_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_a_id",
            "user_b_id",
            name="uq_dm_thread_org_pair",
        ),
        sa.CheckConstraint(
            "user_a_id < user_b_id",
            name="ck_dm_thread_normalized_pair",
        ),
    )


class DmMessage(Base):
    """One message inside a :class:`DmThread`."""

    __tablename__ = "dm_message"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer,
        ForeignKey("dm_thread.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    content = Column(Text, nullable=False)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_dm_message_thread_id_id", "thread_id", "id"),)


# Account table (for external providers like Google, GitHub)
# Each user can have multiple accounts
class Account(Base):
    __tablename__ = "account"

    id = Column(String, primary_key=True, default=_new_string_uuid)
    user_id = Column(String, ForeignKey("user.id", ondelete="CASCADE"))
    provider = Column(String, nullable=False)  # OAuth provider name
    provider_type = Column(String, nullable=False)
    provider_account_id = Column(String, nullable=False)
    access_token = Column(String)  # OAuth access token (optional)
    # TODO: This can be removed? refreshtokens
    refresh_token = Column(String)  # OAuth refresh token (optional)
    # Expiration time for OAuth token (optional)
    expires_at = Column(TIMESTAMP)


class EmailAccount(Base):
    """
    Email/password credentials for a user.

    Users who only use OAuth will have no row here. One row per user maximum.
    The email address itself is not duplicated — it is always read from User.email.
    """

    __tablename__ = "email_account"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    password_hash = Column(String, nullable=False)  # argon2id hash
    email_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )  # Safety-net default; set to True at creation after verification
    password_changed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )  # Set on every password change; used for session invalidation
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # ORM relationship
    user = relationship("User", backref=backref("email_account", uselist=False))


class EmailVerification(Base):
    """
    Short-lived verification codes for signup and password reset.

    During signup, this table also serves as temporary storage for the user's
    credentials until their email is verified — no User or EmailAccount row is
    created until verification succeeds.

    Row lifecycle: rows are always deleted on success (both signup and password
    reset). Expired rows are cleaned up by a periodic job.
    """

    __tablename__ = "email_verification"

    id = Column(Integer, primary_key=True)
    email = Column(
        String,
        nullable=False,
        index=True,
    )  # Not a FK — user may not exist yet (signup)
    code_hash = Column(String, nullable=False)  # SHA-256 hash of the 6-digit code
    purpose = Column(String, nullable=False)  # "signup" | "password_reset"
    password_hash = Column(
        String,
        nullable=True,
    )  # argon2id hash — only for purpose="signup"
    name = Column(String, nullable=True)  # User's first name — only for signup
    last_name = Column(String, nullable=True)  # User's last name — only for signup
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )  # Max 5 attempts before invalidation
    token_jti = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())


class PhoneVerification(Base):
    """
    Short-lived verification codes for phone / WhatsApp number ownership.

    When a user wants to add or change their phone_number or whatsapp_number
    on the User table, they must first verify ownership via SMS.  The flow:

    1. ``POST /user/phone/send-verification`` creates a row with a hashed
       6-digit code and sends the SMS via the communication service.
    2. ``POST /user/phone/confirm-verification`` checks the code, and on
       success sets ``verified_at``.
    3. ``PUT /user`` (profile update) checks for a recent verified row
       matching the new number before accepting the change.

    Rows are deleted after successful profile update or by a periodic
    cleanup job for expired entries.
    """

    __tablename__ = "phone_verifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phone_number = Column(String, nullable=False)
    phone_type = Column(String, nullable=False)  # "phone" | "whatsapp"
    code_hash = Column(String, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    verified_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class MFACredential(Base):
    """
    Polymorphic MFA credential.

    For TOTP, one row per user (the same secret can be scanned into multiple
    authenticator apps). For WebAuthn (future), one row per registered device.

    ``credential_data`` is an encrypted JSON blob whose structure depends on
    ``method_type`` (e.g. ``{"secret": "BASE32..."}`` for TOTP).
    """

    __tablename__ = "mfa_credential"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    method_type = Column(String, nullable=False)  # "totp", "webauthn", "sms"
    credential_data = Column(sa.LargeBinary, nullable=False)  # Encrypted JSON blob
    enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    confirmed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (Index("ix_mfa_credential_user_type", "user_id", "method_type"),)

    # ORM relationship
    user = relationship("User", backref=backref("mfa_credentials", lazy="dynamic"))


class MFARecovery(Base):
    """
    Recovery codes for MFA.

    Tied to the user (not to a specific MFA method). 10 codes generated
    per setup, each 8 alphanumeric characters. Stored as SHA-256 hashes.
    Each code is single-use.
    """

    __tablename__ = "mfa_recovery"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = Column(String, nullable=False)  # SHA-256 hash
    used = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    used_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # ORM relationship
    user = relationship("User", backref=backref("mfa_recovery_codes", lazy="dynamic"))


class Organization(Base):
    """
    Organization model.

    Billing fields live on BillingAccount (linked via billing_account_id FK).
    Business profile fields (tax_id, billing_address, etc.) also live on BillingAccount.
    """

    __tablename__ = "organization"

    id = Column(Integer, primary_key=True)
    owner_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # === BILLING (via BillingAccount) ===
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    image = Column(String, nullable=True)

    # Timezone for org-level billing (IANA format, e.g., "America/New_York")
    # Initialized from owner's timezone on creation, defaults to UTC if not set
    timezone = Column(String, nullable=True)

    # Monthly spending limit for all users/assistants in the org (NULL = no limit)
    monthly_spending_cap = Column(Numeric, nullable=True)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # === MFA ENFORCEMENT ===
    # When True, all email/password members must enable MFA to access this org
    require_mfa = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    org_wide_sharing_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    org_wide_sharing_team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="SET NULL"),
        nullable=True,
    )

    # === VERIFICATION FIELDS ===
    # Verified orgs get higher rate limits
    verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether org has been manually verified by admin",
    )
    verified_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When the org was verified",
    )

    # === FREE TRIAL ===
    free_trial = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    @property
    def data_sharing_mode(self) -> str:
        return "shared" if self.org_wide_sharing_enabled else "private"

    # Relationships
    billing_account = relationship(
        "BillingAccount",
        foreign_keys=[billing_account_id],
    )
    interfaces = relationship(
        "Interface",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OrganizationMember(Base):
    __tablename__ = "organization_member"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="RESTRICT"),
        nullable=False,
    )  # RBAC role for this member (Owner, Admin, Member, Viewer, or custom roles)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Monthly spending limit for this member within this org (NULL = no limit)
    # Set by org admins; cannot exceed org's monthly_spending_cap
    monthly_spending_cap = Column(Numeric, nullable=True)
    # When the spending cap was last changed (for notification deduplication)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)


class OrganizationInvite(Base):
    """Model for pending organization invitations.

    Invites are deleted when accepted or declined.
    Expired invites are cleaned up via admin endpoint.
    """

    __tablename__ = "organization_invite"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token = Column(String, unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invitee_email = Column(String, nullable=False, index=True)
    invitee_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )  # Set if user already exists in system
    invited_by_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="RESTRICT"),
        nullable=False,
    )  # Role to assign when invite is accepted
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


CONTACT_MEMBERSHIP_SCOPE_PERSONAL = "personal"
CONTACT_MEMBERSHIP_SCOPE_TEAM = "team"
CONTACT_MEMBERSHIP_RELATIONSHIP_SELF = "self"
CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS = "boss"
CONTACT_MEMBERSHIP_RELATIONSHIP_COWORKER = "coworker"
CONTACT_MEMBERSHIP_RELATIONSHIP_OTHER = "other"


class ContactMembership(Base):
    """Assistant-specific relationship and policy metadata for a contact.

    Contact rows live in personal or shared log-backed contexts. This table
    stores the assistant-owned overlay that points at one contact id and names
    the root where that contact id is meaningful.
    """

    __tablename__ = "contact_memberships"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    authoring_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id = Column(Integer, nullable=False)
    target_scope = Column(Text, nullable=False)
    target_team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=True,
    )
    relationship = Column(Text, nullable=False)
    should_respond = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    response_policy = Column(
        Text,
        nullable=False,
        default="standard",
        server_default="standard",
    )
    can_edit = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    assistant = orm_relationship(
        "Assistant",
        back_populates="contact_memberships",
        foreign_keys=[assistant_id],
    )
    authoring_assistant = orm_relationship(
        "Assistant",
        foreign_keys=[authoring_assistant_id],
    )
    target_team = orm_relationship("Team", back_populates="contact_memberships")

    __table_args__ = (
        sa.CheckConstraint(
            "target_scope IN ('personal', 'team')",
            name="ck_contact_memberships_target_scope",
        ),
        sa.CheckConstraint(
            "(target_scope = 'personal' AND target_team_id IS NULL) OR "
            "(target_scope = 'team' AND target_team_id IS NOT NULL)",
            name="ck_contact_memberships_scope_target_consistency",
        ),
        sa.CheckConstraint(
            "relationship IN ('self', 'boss', 'coworker', 'other')",
            name="ck_contact_memberships_relationship",
        ),
        Index("ix_contact_memberships_assistant_id", "assistant_id"),
        Index(
            "ix_contact_memberships_authoring_assistant_id",
            "authoring_assistant_id",
            postgresql_where=text("authoring_assistant_id IS NOT NULL"),
        ),
        Index(
            "ix_contact_memberships_assistant_personal_self",
            "assistant_id",
            postgresql_where=text(
                "target_scope = 'personal' AND relationship = 'self'",
            ),
        ),
        Index(
            "ux_contact_memberships_personal_pair",
            "assistant_id",
            "contact_id",
            unique=True,
            postgresql_where=text("target_scope = 'personal'"),
        ),
        Index(
            "ix_contact_memberships_target_team_id",
            "target_team_id",
            postgresql_where=text("target_team_id IS NOT NULL"),
        ),
        Index(
            "ix_contact_memberships_assistant_team_target",
            "assistant_id",
            "target_team_id",
            postgresql_where=text("target_scope = 'team'"),
        ),
        Index(
            "ux_contact_memberships_team_pair",
            "assistant_id",
            "contact_id",
            "target_team_id",
            unique=True,
            postgresql_where=text("target_scope = 'team'"),
        ),
    )


class Permission(Base):
    """Model for permissions (atomic actions like 'project:read', 'interface:edit')."""

    __tablename__ = "permission"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)  # e.g., "project:read"
    description = Column(String, nullable=True)
    resource_type = Column(String, nullable=False)  # e.g., "project", "interface"
    action = Column(String, nullable=False)  # e.g., "read", "write", "delete"
    created_at = Column(TIMESTAMP, server_default=func.now())


class Role(Base):
    """Model for roles within organizations."""

    __tablename__ = "role"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)  # e.g., "Owner", "Admin", "Member", "Viewer"
    description = Column(String, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,  # NULL = system role, available to all orgs
    )
    is_system_role = Column(
        Boolean,
        server_default="f",
        nullable=False,
    )  # True for built-in roles
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    permissions = relationship(
        "Permission",
        secondary="role_permission",
        backref="roles",
    )

    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="uq_role_name_org"),
    )


class RolePermission(Base):
    """Join table for Role-Permission many-to-many relationship."""

    __tablename__ = "role_permission"

    id = Column(Integer, primary_key=True)
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission_id = Column(
        Integer,
        ForeignKey("permission.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )


TEAM_STATUS_ACTIVE = "active"
TEAM_STATUS_DELETING = "deleting"


class Team(Base):
    """Model for teams within organizations."""

    __tablename__ = "team"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(
        Text,
        nullable=False,
        default=TEAM_STATUS_ACTIVE,
        server_default=TEAM_STATUS_ACTIVE,
    )
    is_org_wide_sharing = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    assistant_memberships = relationship(
        "TeamAssistantMembership",
        back_populates="team",
        cascade="all, delete-orphan",
    )
    contact_memberships = relationship(
        "ContactMembership",
        back_populates="target_team",
    )

    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="uq_team_name_org"),
    )


class TeamAssistantMembership(Base):
    """Live membership connecting an assistant to an organization team."""

    __tablename__ = "team_assistant_memberships"

    team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=False,
    )
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    added_by = Column(String, nullable=False)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    team = relationship("Team", back_populates="assistant_memberships")
    assistant = relationship("Assistant", back_populates="team_memberships")

    __table_args__ = (
        sa.PrimaryKeyConstraint("team_id", "assistant_id"),
        Index("ix_team_assistant_memberships_assistant_id", "assistant_id"),
    )


class TeamMember(Base):
    """Join table for Team-User many-to-many relationship."""

    __tablename__ = "team_member"

    id = Column(Integer, primary_key=True)
    team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)


class ResourceAccess(Base):
    """Model for resource-level access control (RBAC)."""

    __tablename__ = "resource_access"

    id = Column(Integer, primary_key=True)
    resource_type = Column(
        String,
        nullable=False,
    )  # e.g., 'project', 'interface', 'tab', 'tile'
    resource_id = Column(Integer, nullable=False)
    role_id = Column(
        Integer,
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
    )
    grantee_type = Column(
        String,
        nullable=False,
    )  # 'user' or 'team'
    grantee_id = Column(
        String,
        nullable=False,
    )  # user_id or team_id (as string)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        # Only one role per grantee per resource (single-role-per-resource)
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            name="uq_resource_access_grantee",
        ),
        Index("idx_resource_access_resource", "resource_type", "resource_id"),
        Index("idx_resource_access_grantee", "grantee_type", "grantee_id"),
    )


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    user_id = Column(String, ForeignKey("user.id", ondelete="CASCADE"))
    organization_id = Column(Integer, ForeignKey("organization.id", ondelete="CASCADE"))
    key = Column(String, unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class Interface(Base):
    __tablename__ = "interface"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # TODO: remove both <user_id> and <organization_id>
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
    )
    name = Column(String(), nullable=False)
    new_counter = Column(Integer, nullable=False)
    items = Column(String(), nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context = Column(String(), nullable=True)
    icon = Column(String(), nullable=False, server_default="folder")
    color = Column(String(), nullable=True)
    order = Column(Integer, nullable=False, server_default="0")
    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    active_tab_id = Column(String, nullable=True)
    # Relationships
    project = relationship("Project")
    user = relationship("User", back_populates="interfaces")
    organization = relationship("Organization", back_populates="interfaces")
    tabs = relationship(
        "Tab",
        back_populates="interface",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "name",
            "is_checkpoint",
            name="it_uq_project_name_checkpoint",
        ),
    )


class AdminUser(Base):
    """Model class for admin users who have special privileges."""

    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationship to User
    user = relationship("User", backref="admin_user")


class FavoriteProject(Base):
    """Model class for user's favorite projects."""

    __tablename__ = "favorite_project"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    position = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_favorite_project"),
    )


class UserDesktop(Base):
    """Registered user desktop machines.

    Each row represents a physical/virtual desktop that a user's desktop app
    has registered after obtaining a public hostname via the tunnel service.
    """

    __tablename__ = "user_desktops"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    os = Column(String, nullable=False)
    # Relay id of this device's raw-TCP SFTP tunnel. The SFTP server is
    # per-device (one rclone serve + one rathole client), so the id lives here
    # rather than on the per-assistant link rows. Server-side teardown
    # (desktop deletion) needs it to deregister the tunnel from the relay.
    sftp_tunnel_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        sa.CheckConstraint(
            "os IN ('ubuntu', 'windows', 'macos')",
            name="ck_user_desktop_os",
        ),
    )


class AssistantUserDesktop(Base):
    """Per-user link between an assistant and a registered user desktop.

    A user links their own machine to the assistants they interact with.  The
    relationship is many-to-many: a single machine can serve several of the
    user's assistants, and a shared (org) assistant can be linked to a separate
    machine for each user who works with it.  Two uniqueness rules apply:

    - ``(assistant_id, owner_user_id)`` -- at most one desktop per assistant
      *per user*, so the runtime can resolve a single target for whoever is
      currently talking to the assistant.
    - ``(assistant_id, user_desktop_id)`` -- a given desktop is linked to a
      given assistant at most once.

    ``owner_user_id`` is denormalised from ``user_desktops.user_id`` so the
    per-user uniqueness constraint and runtime lookups stay single-table.
    """

    __tablename__ = "assistant_user_desktops"

    id = Column(Integer, primary_key=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_desktop_id = Column(
        Integer,
        ForeignKey("user_desktops.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filesys_sync = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # On-demand SFTP access to the user's home for this link. The private key
    # never leaves Orchestra except to the CM via the admin assistant read; only
    # the derived public key is installed on the device.
    filesync_sshkey = Column(String, nullable=True)
    sftp_tunnel_host = Column(String, nullable=True)
    sftp_tunnel_port = Column(Integer, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    desktop = relationship("UserDesktop")
    assistant = relationship("Assistant", back_populates="user_desktop_links")

    __table_args__ = (
        UniqueConstraint(
            "assistant_id",
            "owner_user_id",
            name="uq_assistant_user_desktop_owner",
        ),
        UniqueConstraint(
            "assistant_id",
            "user_desktop_id",
            name="uq_assistant_user_desktop_pair",
        ),
    )


class Assistant(Base):
    """Model class for the assistants table.

    Assistants can be personal (user_id set, organization_id NULL),
    organizational (organization_id set, user_id is the creator), or
    team-owned (owner_team_id set): the team is the product-level owner,
    the assistant's only memory is the team's shared root (no personal
    ``{user}/{agent}`` contexts are ever provisioned), and ``user_id``
    is demoted to the hiring member — a creator/billing/API-key anchor,
    not a supervisor.

    Contact details (phone, email, WhatsApp) are stored in the
    ``assistant_contacts`` table (see :class:`AssistantContact`).
    """

    __tablename__ = "assistants"

    agent_id = Column(Integer, primary_key=True)
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Owning team for team-owned assistants (NULL = user-owned). RESTRICT:
    # a team that owns assistants cannot be deleted until they are deleted
    # or transferred.
    owner_team_id = Column(
        Integer,
        ForeignKey("team.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    first_name = Column(String, nullable=True)
    surname = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    age = Column(Integer, nullable=True)
    nationality = Column(String, nullable=True)
    profile_photo = Column(String, nullable=True)
    profile_video = Column(String, nullable=True)
    desktop_mode = Column(String, nullable=True)
    managed_desktop_status = Column(String, nullable=True)
    managed_desktop_monthly_cost = Column(Numeric, nullable=True)
    managed_desktop_last_billed_month = Column(String, nullable=True)
    managed_desktop_grace_period_started_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    managed_desktop_enabled_at = Column(TIMESTAMP(timezone=True), nullable=True)
    desktop_filesync_sshkey = Column(String, nullable=True)
    about = Column(String, nullable=True)
    timezone = Column(String, nullable=True)
    weekly_limit = Column(Numeric, nullable=True)
    # Monthly spending limit for this assistant (NULL = no limit)
    # Cannot exceed the user's monthly_spending_cap
    monthly_spending_cap = Column(Numeric, nullable=True)
    # When the spending cap was last changed (for notification deduplication)
    monthly_spending_cap_set_at = Column(TIMESTAMP(timezone=True), nullable=True)
    max_parallel = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    # Re-engagement tracking. last_correspondence_at is touched on any
    # inbound/outbound message across all contacts; last_followup_sent_at
    # records when the inactivity re-engagement follow-up fired and is
    # cleared when fresh activity resumes (re-arming the follow-up);
    # inactivity_followup_opted_out is set when the boss explicitly asks
    # not to be followed up with again, and excludes this Coordinator
    # from the routine until it is cleared.
    last_correspondence_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        server_default=func.now(),
        index=True,
    )
    last_followup_sent_at = Column(TIMESTAMP(timezone=True), nullable=True)
    inactivity_followup_opted_out = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    voice_id = sa.Column(
        sa.String,
        nullable=True,
        index=True,
    )
    voice_provider = Column(String, nullable=True)
    # Default LLM for the assistant runtime, as a unillm ``model@provider``
    # endpoint plus a reasoning-effort level. NULL = platform default.
    default_model = Column(String, nullable=True)
    default_reasoning_effort = Column(String, nullable=True)
    # ConversationManager slow-brain LLM. NULL = platform slow-brain default
    # (SLOW_BRAIN_MODEL), independent of default_model / UNIFY_MODEL.
    slow_brain_model = Column(String, nullable=True)
    slow_brain_reasoning_effort = Column(String, nullable=True)
    is_local = Column(Boolean, nullable=False, default=False, server_default="false")
    is_coordinator = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    console_config = relationship(
        "AssistantConsoleConfig",
        uselist=False,
        back_populates="assistant",
        cascade="all, delete-orphan",
    )
    team_memberships = relationship(
        "TeamAssistantMembership",
        back_populates="assistant",
        passive_deletes=True,
    )
    user_desktop_links = relationship(
        "AssistantUserDesktop",
        back_populates="assistant",
        passive_deletes=True,
    )
    contact_memberships = relationship(
        "ContactMembership",
        back_populates="assistant",
        foreign_keys="[ContactMembership.assistant_id]",
        passive_deletes=True,
    )

    @validates("is_coordinator")
    def _validate_is_coordinator_immutable(self, key, value):
        """Once an assistant has been persisted, its coordinator-ness is fixed.

        Mirrors the invariant the org/workspace partial unique indexes
        encode: a row's coordinator status is decided at insert time and
        downstream code should never flip it.
        """
        state = sa_inspect(self)
        if state.persistent and getattr(self, key, None) != value:
            raise ValueError(
                "is_coordinator is immutable after the assistant has been persisted",
            )
        return value

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "voice_id", "voice_provider"],
            ["voices.user_id", "voices.voice_id", "voices.provider"],
            name="fk_assistants_voices",
        ),
        sa.CheckConstraint(
            "desktop_mode IN ('ubuntu', 'windows', 'macos')",
            name="ck_assistant_desktop_mode",
        ),
        Index(
            "ux_assistants_one_personal_coordinator_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_coordinator AND organization_id IS NULL"),
        ),
        Index(
            "ux_assistants_one_workspace_coordinator_per_membership",
            "user_id",
            "organization_id",
            unique=True,
            postgresql_where=text("is_coordinator AND organization_id IS NOT NULL"),
        ),
    )


class AssistantConsoleConfig(Base):
    """Per-assistant UI/UX configuration for forward-deployed Console views.

    One-to-one with ``Assistant``.  Stores typed layout, tab-visibility,
    and theme override fields so the Console can render client-specific
    dashboard-centric (or other) layouts without a JSONB grab-bag.

    Created / updated by unity-deploy's startup hook via
    ``PATCH /admin/assistant/{id}``.
    """

    __tablename__ = "assistant_console_config"

    id = Column(Integer, primary_key=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    version = Column(String, nullable=False, server_default="1")
    layout_mode = Column(String, nullable=False, server_default="standard")
    layout_default_tab = Column(String, nullable=True)
    tabs_hidden = Column(JSONB, nullable=True)
    tabs_order = Column(JSONB, nullable=True)
    theme_brand_name = Column(String, nullable=True)
    theme_accent_color = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    assistant = relationship("Assistant", back_populates="console_config")


class AssistantContact(Base):
    """Tracks provisioned contact details for assistants.

    Each row represents a single provisioned resource (phone, email, or
    WhatsApp sender) with metadata for billing and lifecycle management.

    Lifecycle statuses:
        active         – resource is provisioned and in use.
        grace_period   – billing account has insufficient credits; resource
                         stays active for up to 14 days while user tops up.
        deleted        – resource has been deprovisioned (soft-delete).
    """

    __tablename__ = "assistant_contacts"

    id = Column(Integer, primary_key=True)

    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "phone", "email", "whatsapp"
    contact_type = Column(String, nullable=False)

    # The actual provisioned value (E.164 phone, email address, WhatsApp number)
    contact_value = Column(String, nullable=False)

    # Provider used for provisioning: "twilio", "google_workspace", etc.
    provider = Column(String, nullable=True)

    # Who provisioned: "platform" (we manage it) vs "user" (BYOD – future)
    provisioned_by = Column(
        String,
        nullable=False,
        default="platform",
        server_default="platform",
    )

    # Country code for phone numbers (affects pricing lookups)
    country_code = Column(String, nullable=True)

    # Lifecycle status
    status = Column(
        String,
        nullable=False,
        default="active",
        server_default="active",
    )

    # Type-specific metadata (JSONB):
    #   phone:    {"sid": "PNxxx", "capabilities": {"voice": true, "sms": true}}
    #   email:    {"workspace_user_id": "...", "domain": "unify.ai"}
    #   whatsapp: {"messaging_service_sid": "MGxxx"}
    metadata_ = Column("metadata", JSONB, nullable=True, default=dict)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # When the grace period started (NULL if not in grace period)
    grace_period_started_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Last month billed (e.g. "2026-03") – prevents double-billing
    last_billed_month = Column(String, nullable=True)

    # Monthly cost in $ at time of last levy (audit trail)
    monthly_cost = Column(Numeric, nullable=True)

    # Relationship
    assistant = relationship(
        "Assistant",
        backref=backref("contacts", passive_deletes=True),
    )

    __table_args__ = (
        # One active contact of each type per assistant
        Index(
            "uq_assistant_contact_type_active",
            "assistant_id",
            "contact_type",
            unique=True,
            postgresql_where=text("status != 'deleted'"),
        ),
        # Prevent duplicate active contact values across all assistants
        # (excludes WhatsApp because pool numbers are shared)
        Index(
            "uq_active_contact_value",
            "contact_value",
            unique=True,
            postgresql_where=text(
                "status != 'deleted' "
                "AND contact_type NOT IN ('whatsapp', 'discord') "
                "AND NOT (contact_type IN ('email', 'phone') "
                "AND COALESCE(metadata ->> 'universal_unity', 'false') = 'true')",
            ),
        ),
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
            name="ck_assistant_contact_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'grace_period', 'deleted')",
            name="ck_assistant_contact_status",
        ),
        sa.CheckConstraint(
            "provisioned_by IN ('platform', 'user')",
            name="ck_assistant_contact_provisioned_by",
        ),
    )


class AssistantContactCost(Base):
    """Monthly and one-time costs for each contact type + provider combination.

    Supports per-country pricing (phone numbers vary by country) and
    per-provider pricing (multiple providers per contact type in the future).
    """

    __tablename__ = "contact_type_costs"

    id = Column(Integer, primary_key=True)

    # "phone", "email", "whatsapp"
    contact_type = Column(String, nullable=False)

    # "twilio", "google_workspace", etc.  NULL = default for that type.
    provider = Column(String, nullable=True)

    # NULL = default pricing, "US", "GB", etc. for country-specific pricing.
    country_code = Column(String, nullable=True)

    # Monthly maintenance cost in $
    monthly_cost = Column(Numeric, nullable=False)

    # One-time setup fee in $
    one_time_cost = Column(
        Numeric,
        nullable=False,
        default=0,
        server_default="0",
    )

    effective_from = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "contact_type",
            "provider",
            "country_code",
            name="uq_contact_cost",
        ),
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp', 'discord', 'managed_desktop')",
            name="ck_contact_type_cost_type",
        ),
    )


class AssistantSecret(Base):
    """External service credentials stored per assistant.

    Used by Communication to persist OAuth tokens (e.g. MICROSOFT_ACCESS_TOKEN,
    GOOGLE_ACCESS_TOKEN) that are written via the REST API and read back
    through the admin assistant response.
    """

    __tablename__ = "assistant_secrets"

    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    secret_name = Column(String, nullable=False)
    secret_value = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (sa.PrimaryKeyConstraint("agent_id", "secret_name"),)


class AssistantWorkspaceFileAccess(Base):
    """Per-assistant, per-provider allowlist gating which Drive / SharePoint /
    OneDrive files and folders a connected workspace account may access.

    The policy is a set of explicit allow/deny ``decisions`` keyed by
    ``(drive_id, item_id)`` plus a ``default_allow`` fallback.  Access for any
    item is resolved by walking from the item up its parent chain: the nearest
    ancestor (or the item itself) carrying an explicit decision wins; absent
    any decision, ``default_allow`` applies.  Newly-added files therefore
    inherit their parent folder's decision, with ``default_allow`` governing
    items at otherwise-undecided locations.

    Each ``decisions`` entry is a JSON object::

        {"drive_id": str, "item_id": str, "allow": bool,
         "kind": "folder" | "file", "name": str, "path": str}
    """

    __tablename__ = "assistant_workspace_file_access"

    agent_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    # "google" | "microsoft"
    provider = Column(String, nullable=False)
    default_allow = Column(Boolean, nullable=False, server_default=sa.text("false"))
    decisions = Column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (sa.PrimaryKeyConstraint("agent_id", "provider"),)


class OneTimeCreditGrantLink(Base):
    """
    Credit grant links that award credits when claimed.

    A link can be single-use (max_claims=1, the default), multi-use
    (max_claims>1), or unlimited (max_claims=NULL) so that it can be
    shared on social media or with a group of prospective users.

    Credits are applied to the billing account that corresponds to the
    claimer's active workspace:
    - Personal API key → user's BillingAccount
    - Organization API key → organization's BillingAccount

    Guards:
    - Per-link budget: number of claims must stay below max_claims (if set).
    - Per-user lifetime: a user can only benefit from one link ever.
    - Per-org lifetime: an organization can only benefit from one link ever.
    """

    __tablename__ = "one_time_credit_grant_link"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token = Column(String, unique=True, index=True, nullable=False)
    name = Column(
        String,
        nullable=True,
        comment="Optional admin-facing label (e.g. outreach channel or campaign)",
    )
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    credit_amount = Column(
        Float,
        nullable=False,
        default=10.0,
        comment="Amount of credits to grant per claim",
    )
    max_claims = Column(
        Integer,
        nullable=True,
        comment="Maximum number of claims allowed (NULL = unlimited)",
    )

    claims = relationship(
        "CreditGrantLinkClaim",
        back_populates="link",
        cascade="all, delete-orphan",
    )


class CreditGrantLinkClaim(Base):
    """
    Records an individual claim against a credit grant link.

    Each row represents one user (or org) successfully redeeming a link.
    """

    __tablename__ = "credit_grant_link_claim"
    __table_args__ = (
        UniqueConstraint("link_id", "user_id", name="uq_claim_link_user"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    link_id = Column(
        String,
        ForeignKey("one_time_credit_grant_link.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(String, ForeignKey("user.id"), nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id"),
        nullable=True,
        index=True,
        comment="Organization that received the credits (NULL = personal claim)",
    )
    claimed_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    link = relationship("OneTimeCreditGrantLink", back_populates="claims")


class ReferralCode(Base):
    """A shareable referral code owned by a user.

    A user may own *multiple* codes (e.g. one per channel/campaign); every
    code resolves back to the same referrer. Generating and sharing many
    links is allowed and harmless — the abuse surface lives entirely on the
    *referee* side: a given user can be referred at most once (enforced by
    ``ReferralAttribution``) and the reward is payment-gated and idempotent.
    """

    __tablename__ = "referral_code"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String, unique=True, index=True, nullable=False)
    referrer_user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    referrer_organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "Org that owns this code — reward credits go to the org's "
            "billing account. NULL = personal code (reward to the user)."
        ),
    )
    label = Column(
        String,
        nullable=True,
        comment="Optional channel/campaign label (e.g. 'twitter')",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    disabled_at = Column(TIMESTAMP(timezone=True), nullable=True)


class ReferralAttribution(Base):
    """Records that a user signed up via a referral code.

    Exactly one row per referee (``uq_referral_referee``): a person can be
    referred only once, regardless of how many links exist or which code
    they clicked. The reward fires at most once, when the referee makes
    their first qualifying paid subscription, and is reversed on
    refund/chargeback.

    Lifecycle: ``pending`` → ``rewarded`` (friend paid) → ``reversed``
    (refund/dispute clawback).
    """

    __tablename__ = "referral_attribution"
    __table_args__ = (UniqueConstraint("referee_user_id", name="uq_referral_referee"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String, nullable=False, index=True)
    referrer_user_id = Column(
        String,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    referrer_organization_id = Column(
        Integer,
        ForeignKey("organization.id"),
        nullable=True,
        index=True,
        comment="Org that earns the reward (copied from the code); NULL = personal",
    )
    referee_user_id = Column(
        String,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    referee_billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id"),
        nullable=True,
        index=True,
        comment="BA whose first paid invoice qualifies the reward",
    )
    referrer_billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id"),
        nullable=True,
        comment="BA the referrer reward was granted to (for clawback)",
    )
    status = Column(
        String,
        nullable=False,
        default="pending",
        server_default="pending",
        comment="pending | rewarded | reversed",
    )
    signup_ip = Column(
        String,
        nullable=True,
        comment="Referee IP at attribution time (velocity/abuse scoring)",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    rewarded_at = Column(TIMESTAMP(timezone=True), nullable=True)
    reversed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    first_payment_invoice_id = Column(
        String,
        nullable=True,
        index=True,
        comment="Stripe invoice id of the friend's qualifying first payment",
    )
    reward_amount = Column(
        Numeric,
        nullable=True,
        comment="Credits granted to the referrer (USD-denominated)",
    )
    referee_bonus_amount = Column(
        Numeric,
        nullable=True,
        comment="Bonus credits granted to the referee (USD-denominated)",
    )


class OnboardingStatus(Base):
    """
    Tracks user onboarding progress.

    The current_step represents WHERE TO RESUME next time:
    - workspace_setup: Initial state – user needs to choose personal vs. organization workspace
    - completed: All onboarding steps done

    step_data accumulates information from completed steps:
    - selected_type: "personal" | "organization"
    - organization_id, organization_name (if organization)
    - completed_at (when completed)
    """

    __tablename__ = "onboarding_status"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    current_step = Column(
        String(50),
        nullable=False,
        # No server_default - handled by DAO to avoid migrations when flow changes
        comment="Next step to resume at (freeform in DB, enforced by API)",
    )
    step_data = Column(
        JSONB,
        nullable=False,
        server_default="{}",
        comment="Accumulated data from completed steps (freeform JSON)",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationship
    user = relationship("User", backref=backref("onboarding_status", uselist=False))


class Voice(Base):
    """Model class for the assistants voices table."""

    __tablename__ = "voices"

    voice_id = Column(
        String,
        primary_key=True,
    )  # This will store the TTS provider's voice ID
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    provider = Column(String, primary_key=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(String, nullable=False)
    gender = Column(String, nullable=True)
    language = Column(String, nullable=False)  # e.g., "en", "es"
    is_preset = Column(
        Boolean,
        nullable=False,
        server_default="f",
    )  # True if this is a Cartesia preset voice

    __table_args__ = (
        sa.PrimaryKeyConstraint("user_id", "voice_id", "provider"),
        sa.CheckConstraint(
            "provider IN ('cartesia', 'elevenlabs')",
            name="ck_voice_provider",
        ),
    )


class Tab(Base):
    """Model class for tabs within interfaces."""

    __tablename__ = "tab"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    interface_id = Column(
        String,
        ForeignKey("interface.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(), nullable=False)
    icon = Column(String(), nullable=False, server_default="tab")
    visible = Column(Boolean(), nullable=False, server_default="t")
    active = Column(Boolean(), nullable=False, server_default="f")
    order = Column(Integer, nullable=False, server_default="0")
    context = Column(String(), nullable=True)
    color = Column(String(), nullable=True)
    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationships
    interface = relationship("Interface", back_populates="tabs")
    tiles = relationship(
        "Tile",
        back_populates="tab",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "interface_id",
            "name",
            "is_checkpoint",
            name="tab_uq_interface_name_checkpoint",
        ),
    )


class Tile(Base):
    """Model class for tiles within tabs."""

    __tablename__ = "tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tab_id = Column(
        String,
        ForeignKey("tab.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(), nullable=False)
    type = Column(
        String(),
        nullable=True,
    )  # "Table", "Plot", "View", "Editor", "Terminal"

    # Position properties
    x_position = Column(Float, nullable=False)
    y_position = Column(Float, nullable=False)
    width = Column(Float, nullable=False)
    height = Column(Float, nullable=False)
    minW = Column(Float, nullable=True)
    minH = Column(Float, nullable=True)

    # Common properties
    visible = Column(Boolean(), nullable=False, server_default="t")
    locked = Column(Boolean(), nullable=False, server_default="f")
    moved = Column(Boolean(), nullable=False, server_default="f")
    static = Column(Boolean(), nullable=False, server_default="f")
    color = Column(String(), nullable=True)

    # Common data properties
    context = Column(String(), nullable=True)
    table = Column(String(), nullable=True)
    auto_update = Column(String(), nullable=True)
    freeze = Column(String(), nullable=True)
    filters = Column(String(), nullable=True)
    common_filter = Column(String(), nullable=True)
    metric = Column(String(), nullable=True)
    column_context = Column(String(), nullable=True)
    grouping = Column(String(), nullable=True)

    # Flag to indicate if this is a checkpoint (manual save) or auto-save
    is_checkpoint = Column(Boolean(), nullable=False, server_default="f")
    # ID of the checkpoint counterpart (if this is the active version)
    # or the active counterpart (if this is a checkpoint)
    checkpoint_or_active_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    # Relationships
    tab = relationship("Tab", back_populates="tiles")
    table_tile = relationship(
        "TableTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    plot_tile = relationship(
        "PlotTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    view_tile = relationship(
        "ViewTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    editor_tile = relationship(
        "EditorTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    terminal_tile = relationship(
        "TerminalTile",
        back_populates="tile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tab_id",
            "name",
            "is_checkpoint",
            name="tile_uq_tab_name_checkpoint",
        ),
    )


class TableTile(Base):
    """Model class for Table-specific tile properties."""

    __tablename__ = "table_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Table-specific properties
    table_type = Column(String(), nullable=True)
    page_number = Column(String(), nullable=True)
    column_order = Column(String(), nullable=True)
    hidden_columns = Column(String(), nullable=True)
    default_hidden_columns = Column(Boolean(), nullable=False, server_default="t")
    sorting = Column(String(), nullable=True)
    group_sorting = Column(String(), nullable=True)
    columns_pin_left = Column(String(), nullable=True)
    columns_pin_right = Column(String(), nullable=True)
    selected = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="table_tile")


class PlotTile(Base):
    """Model class for Plot-specific tile properties."""

    __tablename__ = "plot_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Plot-specific properties
    plot_type = Column(String(), nullable=True)
    plot_scale_x = Column(String(), nullable=True)
    plot_scale_y = Column(String(), nullable=True)
    plot_aggregate = Column(String(), nullable=True)
    x_axis = Column(String(), nullable=True)
    y_axis = Column(String(), nullable=True)
    plot_group_by = Column(String(), nullable=True)
    plot_group_by_colors = Column(String(), nullable=True)
    bin_count = Column(String(), nullable=True)
    regression_line = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="plot_tile")


class ViewTile(Base):
    """Model class for View-specific tile properties."""

    __tablename__ = "view_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # View-specific properties
    base_index = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="view_tile")


class EditorTile(Base):
    """Model class for Editor-specific tile properties."""

    __tablename__ = "editor_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Editor-specific properties
    file_name = Column(String(), nullable=True)
    file_type = Column(String(), nullable=True)
    content = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="editor_tile")


class TerminalTile(Base):
    """Model class for Terminal-specific tile properties."""

    __tablename__ = "terminal_tile"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tile_id = Column(
        String,
        ForeignKey("tile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Terminal-specific properties
    shell_type = Column(String(), nullable=True)

    # Relationships
    tile = relationship("Tile", back_populates="terminal_tile")


class Plot(Base):
    """Model class for shareable plot configurations.

    Plots are linked to projects and follow project-based access control.
    When a project is deleted, all associated plots are cascade deleted.
    """

    __tablename__ = "plot"

    id = Column(Integer, primary_key=True)
    token = Column(String(12), unique=True, nullable=False, index=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = Column(String, nullable=True)
    plot_config = Column(JSONB, nullable=False)
    project_config = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships - passive_deletes=True lets the DB handle CASCADE DELETE
    project = relationship("Project", backref=backref("plots", passive_deletes=True))
    context = relationship("Context", backref=backref("plots", passive_deletes=True))

    __table_args__ = (
        Index("idx_plot_project_id", "project_id"),
        Index("idx_plot_context_id", "context_id"),
        Index("idx_plot_user_id", "user_id"),
        Index("idx_plot_organization_id", "organization_id"),
    )


class TableView(Base):
    """Model class for shareable table view configurations.

    TableViews are linked to projects and follow project-based access control.
    When a project is deleted, all associated table views are cascade deleted.
    """

    __tablename__ = "table_view"

    id = Column(Integer, primary_key=True)
    token = Column(String(12), unique=True, nullable=False, index=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = Column(String, nullable=True)
    table_config = Column(JSONB, nullable=False)
    project_config = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships - passive_deletes=True lets the DB handle CASCADE DELETE
    project = relationship(
        "Project",
        backref=backref("table_views", passive_deletes=True),
    )
    context = relationship(
        "Context",
        backref=backref("table_views", passive_deletes=True),
    )

    __table_args__ = (
        Index("idx_table_view_project_id", "project_id"),
        Index("idx_table_view_context_id", "context_id"),
        Index("idx_table_view_user_id", "user_id"),
        Index("idx_table_view_organization_id", "organization_id"),
    )


class SpendingLimitNotification(Base):
    """
    Tracks spending limit notifications to prevent duplicate emails.

    When a spending limit is reached, we record the notification here.
    Subsequent limit breaches for the same (entity_type, entity_id, month, limit_value)
    are deduplicated unless the limit was re-configured (limit_set_at > notified_at).

    This table is intentionally NOT linked via foreign keys to entity tables
    so that notification records are preserved when entities are deleted (audit trail).
    """

    __tablename__ = "spending_limit_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Which entity hit the limit
    entity_type = Column(
        String(20),
        nullable=False,
        comment="'assistant', 'user', 'member', or 'organization'",
    )
    entity_id = Column(
        String,
        nullable=False,
        comment="ID of the entity (agent_id, user_id, or org_id)",
    )

    # When and at what limit
    month = Column(
        String(7),
        nullable=False,
        comment="Billing month in YYYY-MM format",
    )
    limit_value = Column(
        Numeric,
        nullable=False,
        comment="The limit value that was reached",
    )

    # When the limit was configured (for re-enable detection)
    limit_set_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="When the limit was configured",
    )

    # Notification metadata
    notified_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    notified_user_ids = Column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="List of user IDs who received the notification email",
    )

    # Entity name for auditing (may become stale if entity renamed)
    entity_name = Column(
        String,
        nullable=True,
        comment="Name of the entity at time of notification (for auditing)",
    )

    # Current spend at time of notification (for auditing)
    current_spend = Column(
        Numeric,
        nullable=True,
        comment="Spend amount when notification was triggered",
    )

    __table_args__ = (
        # Index for deduplication lookups
        Index(
            "ix_spending_limit_notifications_dedupe",
            "entity_type",
            "entity_id",
            "month",
            "limit_value",
        ),
        # Index for entity lookups
        Index(
            "ix_spending_limit_notifications_entity",
            "entity_type",
            "entity_id",
        ),
        # Index for cleanup queries
        Index(
            "ix_spending_limit_notifications_month",
            "month",
        ),
    )


class RateLimitCounter(Base):
    """
    Tracks API request counts in 5-minute time buckets for rate limiting.

    This table replaces the previous approval-based gating with a flexible
    rate limiting system. It supports:
    - Category-based limits (assistant_hiring, assistant_media, assistant_crud, assistant_voice)
    - Optional per-endpoint overrides
    - User-level and organization-level (shared) limits
    - Rolling 24-hour window calculation
    """

    __tablename__ = "rate_limit_counter"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Who made the request
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # What endpoint/category
    endpoint_category = Column(
        String(50),
        nullable=False,
        comment="Rate limit category: 'assistant_hiring', 'assistant_media', 'assistant_crud', 'assistant_voice'",
    )
    endpoint_path = Column(
        String(200),
        nullable=True,
        comment="Specific endpoint path for per-endpoint overrides",
    )

    # When (5-minute buckets)
    time_bucket = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        comment="Start of the 5-minute time bucket",
    )

    # Request count
    request_count = Column(
        Integer,
        nullable=False,
        server_default="1",
        comment="Number of requests in this bucket",
    )

    __table_args__ = (
        # Unique constraint for upsert operations
        UniqueConstraint(
            "user_id",
            "endpoint_category",
            "endpoint_path",
            "time_bucket",
            name="uq_rate_limit_counter",
        ),
        # Index for user + category lookups
        Index(
            "ix_rate_limit_counter_user_category",
            "user_id",
            "endpoint_category",
            "time_bucket",
        ),
        # Index for organization-level lookups
        Index(
            "ix_rate_limit_counter_org_category",
            "organization_id",
            "endpoint_category",
            "time_bucket",
        ),
        # Index for endpoint-specific lookups
        Index(
            "ix_rate_limit_counter_endpoint",
            "user_id",
            "endpoint_path",
            "time_bucket",
        ),
        # Index for cleanup queries
        Index(
            "ix_rate_limit_counter_time_bucket",
            "time_bucket",
        ),
    )


class AuthRateLimitEntry(Base):
    """
    IP-based rate limiting for unauthenticated auth endpoints.

    Unlike RateLimitCounter (which keys on user_id), this table keys on
    a composite string of IP + identifier (email, user_id, or just IP)
    to throttle login attempts, MFA brute-force, registration spam, etc.
    """

    __tablename__ = "auth_rate_limit_entry"

    id = Column(Integer, primary_key=True, autoincrement=True)

    key = Column(
        String(500),
        nullable=False,
        index=True,
        comment="Composite key: 'ip:identifier' or just 'ip'",
    )
    endpoint_category = Column(
        String(50),
        nullable=False,
        comment="Auth rate limit category (auth_login, auth_mfa, auth_register, ...)",
    )
    time_bucket = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        comment="Start of the 5-minute time bucket",
    )
    attempt_count = Column(
        Integer,
        nullable=False,
        server_default="1",
    )

    __table_args__ = (
        UniqueConstraint(
            "key",
            "endpoint_category",
            "time_bucket",
            name="uq_auth_rate_limit_entry",
        ),
        Index(
            "ix_auth_rate_limit_key_category",
            "key",
            "endpoint_category",
            "time_bucket",
        ),
        Index(
            "ix_auth_rate_limit_time_bucket",
            "time_bucket",
        ),
    )


class ApiMessage(Base):
    """
    Tracks programmatic API messages sent to assistants.

    Each row represents a single request-response exchange: a developer sends a
    message via the REST API, and the assistant may (or may not) respond.
    The polling endpoint reads from this table.
    """

    __tablename__ = "api_messages"

    id = Column(String, primary_key=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id = Column(Integer, nullable=True)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="processing")
    response = Column(String, nullable=True)
    tags = Column(JSONB, nullable=True, server_default="[]")
    attachments = Column(JSONB, nullable=True, server_default="[]")
    response_tags = Column(JSONB, nullable=True)
    response_attachments = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)


class SharedPoolNumber(Base):
    """Platform-owned contact identifiers shared across assistants.

    Each row represents a registered sender (e.g. a Twilio WhatsApp number,
    an Instagram bot account) that can be assigned to multiple assistants.
    The pool is small and managed at the platform level.
    """

    __tablename__ = "shared_pool_numbers"

    id = Column(Integer, primary_key=True)
    platform = Column(
        String,
        nullable=False,
        default="whatsapp",
        server_default="whatsapp",
    )
    number = Column(String, nullable=False)
    status = Column(String, nullable=False, default="active", server_default="active")
    twilio_sender_sid = Column(String, nullable=True)
    auth_token = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "platform",
            "number",
            name="uq_shared_pool_number_platform_number",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_shared_pool_number_status",
        ),
    )


class SharedPlatformRoute(Base):
    """Maps (pool_number, external_contact) → assistant for inbound routing.

    Only used for external contacts (non-platform-users).  Platform users
    are routed dynamically via user identity lookups (Tier 1).
    Routes are created when an assistant sends an outbound message
    to an external contact, establishing a permanent reply path.
    """

    __tablename__ = "shared_platform_routes"

    id = Column(Integer, primary_key=True)
    pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_number = Column(String, nullable=False)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_inbound_at = Column(TIMESTAMP(timezone=True), nullable=True)

    call_permission_status = Column(String, nullable=True)
    call_permission_requested_at = Column(TIMESTAMP(timezone=True), nullable=True)
    call_permission_granted_at = Column(TIMESTAMP(timezone=True), nullable=True)
    call_permission_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    call_permission_last_provider_event_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    call_permission_source = Column(String, nullable=True)
    pending_whatsapp_call_context = Column(Text, nullable=True)
    pending_whatsapp_call_context_at = Column(TIMESTAMP(timezone=True), nullable=True)

    pool_number = relationship("SharedPoolNumber")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "pool_number_id",
            "contact_number",
            name="uq_pool_contact",
        ),
        Index(
            "ix_shared_routes_assistant",
            "assistant_id",
            "contact_number",
        ),
        Index(
            "ix_shared_routes_contact",
            "contact_number",
        ),
    )


class DecommissionedRoute(Base):
    """Tracks old (pool_number, contact) pairs after conflict reassignment.

    When an assistant is reassigned to a new pool number, its old routes
    are recorded here so that inbound messages to the old number can be
    answered with an auto-reply instead of being silently dropped.
    """

    __tablename__ = "decommissioned_routes"

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False)
    pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_identifier = Column(String, nullable=False)
    old_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    new_pool_number_id = Column(
        Integer,
        ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
        nullable=True,
    )
    decommissioned_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    pool_number = relationship("SharedPoolNumber", foreign_keys=[pool_number_id])
    new_pool_number = relationship(
        "SharedPoolNumber",
        foreign_keys=[new_pool_number_id],
    )

    __table_args__ = (
        Index(
            "ix_decommissioned_routes_lookup",
            "pool_number_id",
            "contact_identifier",
        ),
    )


class CommunicationCallSession(Base):
    """Durable routing state for provider voice-call callbacks."""

    __tablename__ = "communication_call_sessions"

    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    provider_call_sid = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
    )
    from_number = Column(String, nullable=False)
    to_number = Column(String, nullable=False)
    pool_number = Column(String, nullable=True)
    conference_name = Column(String, nullable=False)
    livekit_room = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="created")
    recording_url = Column(String, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=func.now())

    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_call_sid",
            name="uq_communication_call_sessions_provider_sid",
        ),
        Index(
            "ix_communication_call_sessions_assistant_channel",
            "assistant_id",
            "channel",
            "created_at",
        ),
        Index(
            "ix_communication_call_sessions_livekit_room",
            "livekit_room",
        ),
    )


class ConflictEvent(Base):
    """Audit log for shared-pool conflict resolutions.

    Records every conflict detection + resolution, including the pool
    reassignments performed and the delivery status of WhatsApp
    notifications sent to affected users.
    """

    __tablename__ = "conflict_events"

    id = Column(Integer, primary_key=True)
    platform = Column(String, nullable=False)
    conflict_type = Column(String, nullable=False)
    trigger_assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="SET NULL"),
        nullable=True,
    )
    affected_assistant_ids = Column(JSONB, nullable=False)
    old_pool_assignments = Column(JSONB, nullable=False)
    new_pool_assignments = Column(JSONB, nullable=False)
    notification_recipients = Column(JSONB, nullable=True)
    notification_status = Column(JSONB, nullable=True)
    status = Column(
        String,
        nullable=False,
        default="notifying",
        server_default="notifying",
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    resolved_at = Column(TIMESTAMP(timezone=True), nullable=True)

    trigger_assistant = relationship("Assistant")

    __table_args__ = (
        sa.CheckConstraint(
            "conflict_type IN ('contact_overlap', 'user_to_user', 'org_membership')",
            name="ck_conflict_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('notifying', 'resolved', 'notification_failed', 'failed')",
            name="ck_conflict_event_status",
        ),
        Index("ix_conflict_events_status", "status"),
        Index("ix_conflict_events_trigger_assistant", "trigger_assistant_id"),
    )


class CreditTransaction(Base):
    """Append-only ledger of every credit movement on a billing account.

    Positive ``amount`` = credits added. Inflow categories:
      * ``recharge`` — one-off PAYG top-up (paid).
      * ``subscription_recharge`` — subscription cycle / plan credits (paid;
        the customer pays the subscription invoice that funds them).
      * ``promo`` — promotional link/code redemption (free).
      * ``grant`` — trial / goodwill award (free).
      * ``refund`` / ``dispute`` / ``carryover`` — adjustments.
    Negative ``amount`` = credits spent (llm, hire, resources, media, seat,
    subscription, forfeit / forfeit_at_conversion).

    The canonical spending set is ``llm | hire | resources | media`` (debits);
    the canonical credit set is
    ``recharge | subscription_recharge | promo | grant | refund | dispute``
    (see ``orchestra.web.api.credits.schema``). Internal reconciliation
    routines may use additional diagnostic categories (e.g. ``void``,
    ``stale_pending_recharge``).

    Note: subscription vs trial credits are *both* expiring grants tagged
    ``detail.grant_kind`` (``"plan"`` / ``"trial"``); the forfeit/expiry logic
    keys off that tag, not the ledger ``category`` — the category split is for
    paid-vs-free reporting only.

    The ledger is intentionally billing-mode-agnostic: the same row shape
    serves CREDITS and METERED accounts. Drift between ledger and wallet
    can be detected on demand by summing signed ``amount`` for the account
    and comparing with ``billing_account.credits`` (CREDITS-mode only).

    Managed billing: ``plan_assignment_id`` denormalises the
    plan assignment that was active when this row was written. Lets the
    metered invoicer attribute usage to the right plan version even when
    plans change mid-period (deferred PRORATE_IMMEDIATELY support), and
    gives audit traceability for the disputed-invoice case. NULL for
    historical rows written before the v2 schema existed.
    """

    __tablename__ = "credit_transaction"

    id = Column(BigInteger, primary_key=True)
    billing_account_id = Column(
        Integer,
        ForeignKey("billing_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Financial
    amount = Column(Numeric, nullable=False)

    # Dimensions (indexed for fast filtering)
    category = Column(String, nullable=False)
    assistant_id = Column(Integer, nullable=True)
    user_id = Column(String, nullable=True)
    organization_id = Column(Integer, nullable=True)

    description = Column(String, nullable=True)
    detail = Column(JSONB, nullable=True)

    plan_assignment_id = Column(
        BigInteger,
        ForeignKey("billing_plan_assignment.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        Index("ix_credit_txn_ba_at", "billing_account_id", "at"),
        Index("ix_credit_txn_ba_category_at", "billing_account_id", "category", "at"),
        Index("ix_credit_txn_assistant_category_at", "assistant_id", "category", "at"),
        Index("ix_credit_txn_user_at", "user_id", "at"),
    )


class AssistantCleanupTask(Base):
    """Retryable cleanup work item for assistant teardown after owner deletion.

    A task is created before an owner row is irreversibly deleted. The payload
    stores the minimum retry state needed to finish runtime teardown, contact
    deprovisioning, and assistant-scoped GCS cleanup outside the original
    request lifecycle.
    """

    __tablename__ = "assistant_cleanup_tasks"

    id = Column(Integer, primary_key=True)
    assistant_id = Column(Integer, nullable=False)
    desktop_mode = Column(String, nullable=True)
    source_flow = Column(String, nullable=False)
    cleanup_payload = Column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    status = Column(String, nullable=False, default="pending", server_default="pending")
    attempt_count = Column(Integer, nullable=False, server_default=sa.text("0"))
    last_error = Column(String, nullable=True)
    last_result = Column(JSONB, nullable=True)
    next_retry_at = Column(TIMESTAMP(timezone=True), nullable=True)
    processing_started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_assistant_cleanup_task_status",
        ),
        Index("ix_assistant_cleanup_tasks_status", "status", "next_retry_at"),
        Index("ix_assistant_cleanup_tasks_assistant", "assistant_id"),
    )


class DashboardToken(Base):
    """Token-to-context mapping for dashboard tiles and layouts.

    Content lives in Unify contexts (Dashboards/Tiles, Dashboards/Layouts);
    this table provides the routing information the console needs to resolve
    a token-based URL to the correct Unify context path and creator identity.
    """

    __tablename__ = "dashboard_token"

    token = Column(String(12), primary_key=True)
    entity_type = Column(String(20), nullable=False)
    context_name = Column(String(500), nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP, server_default=func.now())

    project = relationship(
        "Project",
        backref=backref("dashboard_tokens", passive_deletes=True),
    )

    __table_args__ = (
        Index("idx_dashboard_token_project_id", "project_id"),
        Index("idx_dashboard_token_user_id", "user_id"),
    )


# Sentinel `thread_ts` value used by ``SlackThreadRoute`` rows that represent
# the *root* of a direct-message conversation. Slack DMs do not carry a real
# thread timestamp; this lets a single unique index cover both channel threads
# and DM roots.
DM_ROOT_SENTINEL = "__dm_root__"


class SlackInstall(Base):
    """Per-workspace Slack OAuth install owned by a Unify org *or* user.

    The owner is polymorphic — exactly one of ``organization_id`` or
    ``user_id`` is set on every row (enforced by
    ``ck_slack_install_one_owner``). This mirrors how :class:`Assistant`
    itself works: assistants are either personal (``user_id`` set,
    ``organization_id`` NULL) or organizational (``organization_id`` set).
    A personal Slack install routes to the user's personal assistants;
    an organizational install routes to the org's assistants. The two
    populations never mix.

    Uniqueness:

    * ``ux_slack_install_org_team`` — at most one row per
      ``(organization_id, slack_team_id)`` (org installs only).
    * ``ux_slack_install_user_team`` — at most one row per
      ``(user_id, slack_team_id)`` (personal installs only).
    * ``ux_slack_install_active_team`` — at most one *active* (non-revoked)
      row per ``slack_team_id``. A Slack workspace can only carry one bot
      identity at a time, so two different owners cannot both hold the same
      workspace live. Revoked rows are kept as an audit trail and don't
      block a different owner from claiming the workspace afterwards.

    Enterprise Grid installs additionally carry ``enterprise_id``; the
    ``slack_team_id`` is still the unit of routing because messages always
    arrive on a workspace.
    """

    __tablename__ = "slack_installs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    slack_team_id = Column(String, nullable=False)
    slack_team_name = Column(String, nullable=True)
    slack_app_id = Column(String, nullable=False)
    enterprise_id = Column(String, nullable=True)
    bot_user_id = Column(String, nullable=False)
    bot_access_token = Column(Text, nullable=False)
    installer_user_id = Column(String, nullable=True)
    scopes = Column(Text, nullable=True)
    installed_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    revoked_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "(organization_id IS NULL) <> (user_id IS NULL)",
            name="ck_slack_install_one_owner",
        ),
        Index(
            "ux_slack_install_org_team",
            "organization_id",
            "slack_team_id",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
        Index(
            "ux_slack_install_user_team",
            "user_id",
            "slack_team_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "ux_slack_install_active_team",
            "slack_team_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_slack_installs_team_id", "slack_team_id"),
    )


class SlackChannelBinding(Base):
    """Default assistant for a Slack channel.

    Created when an assistant is explicitly invited to a channel (or via
    admin endpoint). Sets the default recipient for untokened mentions in
    that channel. Coordinators do not need bindings — they are the
    organization-wide fallback.
    """

    __tablename__ = "slack_channel_bindings"

    id = Column(Integer, primary_key=True)
    install_id = Column(
        Integer,
        ForeignKey("slack_installs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = Column(String, nullable=False)
    channel_name = Column(String, nullable=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bound_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    install = relationship("SlackInstall")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "install_id",
            "channel_id",
            name="uq_slack_channel_binding",
        ),
    )


class SlackThreadRoute(Base):
    """Sticky routing for a single Slack conversation.

    Carries two distinct kinds of rows, distinguished only by ``thread_ts``:

    * **Channel threads** — ``thread_ts`` is the Slack root timestamp of the
      thread (``"1709315643.123456"``). Inserted on the first explicit
      ``<@app> <token>`` mention inside a thread *or* on the first outbound
      reply the assistant sends. All subsequent un-tokened messages in the
      thread inherit the assistant.

    * **DM roots** — ``thread_ts`` is :data:`DM_ROOT_SENTINEL`. One row per
      ``(install, dm_channel)``. Inserted on first assistant-initiated DM or
      first explicit token-in-DM by the user; re-routes the whole DM
      thereafter.

    Rows expire after a configurable TTL (default 14 days, refreshed on
    every send/receive that hits the route).
    """

    __tablename__ = "slack_thread_routes"

    id = Column(Integer, primary_key=True)
    install_id = Column(
        Integer,
        ForeignKey("slack_installs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = Column(String, nullable=False)
    thread_ts = Column(String, nullable=False)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_used_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)

    install = relationship("SlackInstall")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "install_id",
            "channel_id",
            "thread_ts",
            name="uq_slack_thread_route",
        ),
        Index("ix_slack_thread_routes_expires", "expires_at"),
    )


class MsTeamsBotInstall(Base):
    """Per-tenant Microsoft Teams (Bot Framework) install.

    The Teams-bot channel is the Bot-Framework analogue of the Slack app:
    a single Azure bot registration (``bot_app_id``) that a company
    installs from the Teams Store, after which inbound activities for that
    Microsoft tenant fan out to the assistants of one Unify owner. The
    routing unit is the Azure AD ``tenant_id`` (analogue of Slack's
    ``slack_team_id``); tokens are minted on demand from the shared app id
    + secret rather than stored per-tenant, so we persist the tenant's
    ``service_url`` (region-specific Bot Framework endpoint) for outbound
    proactive replies instead of a bot token.

    Ownership is deferred, unlike Slack. A Teams Store install gives us the
    ``tenant_id`` before we know which Unify organization it belongs to, so
    a row is created *pending* (no owner, carrying a ``bind_nonce``) on the
    first ``conversationUpdate`` and later bound to an owner via the
    tenant-to-org handshake (Console → ``POST /admin/ms-teams-bot/bind``).
    The owner column pair therefore allows the transient both-NULL state:

    * pending — ``organization_id`` and ``user_id`` both NULL,
      ``bind_nonce`` set, ``bound_at`` NULL. Dispatch drops traffic for a
      pending install (nothing to route to yet).
    * bound — exactly one of ``organization_id`` / ``user_id`` set,
      ``bind_nonce`` NULL, ``bound_at`` populated. Routes like a Slack
      install.

    ``ck_ms_teams_bot_install_single_owner`` forbids *both* owners being
    set at once (at most one owner) while permitting the pending state.

    Uniqueness mirrors ``SlackInstall``:

    * ``ux_ms_teams_bot_install_org_tenant`` — one *active* (non-revoked)
      row per ``(organization_id, tenant_id)`` (org installs).
    * ``ux_ms_teams_bot_install_user_tenant`` — one *active* (non-revoked)
      row per ``(user_id, tenant_id)`` (personal installs).
    * ``ux_ms_teams_bot_install_active_tenant`` — at most one *active*
      (non-revoked) row per ``tenant_id``.

    The owner-tenant indexes are scoped to ``revoked_at IS NULL`` so a
    revoked install (kept for audit) never blocks a fresh re-install/bind
    for the same owner and tenant — reconnecting after a disconnect is a
    clean insert, not a unique-constraint collision.
    """

    __tablename__ = "ms_teams_bot_installs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tenant_id = Column(String, nullable=False)
    tenant_name = Column(String, nullable=True)
    bot_app_id = Column(String, nullable=False)
    service_url = Column(String, nullable=True)
    installer_aad_object_id = Column(String, nullable=True)
    bind_nonce = Column(String, nullable=True)
    bound_at = Column(TIMESTAMP(timezone=True), nullable=True)
    scopes = Column(Text, nullable=True)
    installed_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    revoked_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "NOT (organization_id IS NOT NULL AND user_id IS NOT NULL)",
            name="ck_ms_teams_bot_install_single_owner",
        ),
        Index(
            "ux_ms_teams_bot_install_org_tenant",
            "organization_id",
            "tenant_id",
            unique=True,
            postgresql_where=text(
                "organization_id IS NOT NULL AND revoked_at IS NULL",
            ),
        ),
        Index(
            "ux_ms_teams_bot_install_user_tenant",
            "user_id",
            "tenant_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL AND revoked_at IS NULL"),
        ),
        Index(
            "ux_ms_teams_bot_install_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_ms_teams_bot_installs_tenant_id", "tenant_id"),
        Index("ix_ms_teams_bot_installs_bind_nonce", "bind_nonce"),
    )


class MsTeamsBotChannelBinding(Base):
    """Default assistant for a Microsoft Teams channel.

    Analogue of :class:`SlackChannelBinding`. ``channel_id`` is the Teams
    channel identity (Bot Framework ``channelData.channel.id``, e.g.
    ``19:...@thread.tacv2``), *not* a per-thread conversation id — a
    binding sets the default recipient for untokened, un-routed traffic in
    that channel. Coordinators need no binding; they are the owner-wide
    fallback.
    """

    __tablename__ = "ms_teams_bot_channel_bindings"

    id = Column(Integer, primary_key=True)
    install_id = Column(
        Integer,
        ForeignKey("ms_teams_bot_installs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = Column(String, nullable=False)
    channel_name = Column(String, nullable=True)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bound_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    install = relationship("MsTeamsBotInstall")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "install_id",
            "channel_id",
            name="uq_ms_teams_bot_channel_binding",
        ),
    )


class MsTeamsBotConversationRoute(Base):
    """Sticky routing for a single Microsoft Teams conversation.

    Analogue of :class:`SlackThreadRoute`, collapsed onto the Bot
    Framework ``conversation.id`` which already uniquely identifies a
    conversation whether it is a 1:1 personal chat, a group chat, or a
    channel reply thread (the channel thread id is encoded in the
    conversation id). One row per ``(install, conversation_id)``.

    ``conversation_reference`` stores the serialized Bot Framework
    ConversationReference JSON (bot + user identities, ``service_url``,
    ``conversation``, tenant) captured on inbound so the outbound path can
    reply *proactively* into the same conversation without the user having
    to message first. Rows expire after a TTL (default 14 days, refreshed
    on every send/receive that hits the route).
    """

    __tablename__ = "ms_teams_bot_conversation_routes"

    id = Column(Integer, primary_key=True)
    install_id = Column(
        Integer,
        ForeignKey("ms_teams_bot_installs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(String, nullable=False)
    assistant_id = Column(
        Integer,
        ForeignKey("assistants.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_reference = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    last_used_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)

    install = relationship("MsTeamsBotInstall")
    assistant = relationship("Assistant")

    __table_args__ = (
        UniqueConstraint(
            "install_id",
            "conversation_id",
            name="uq_ms_teams_bot_conversation_route",
        ),
        Index("ix_ms_teams_bot_conversation_routes_expires", "expires_at"),
    )
