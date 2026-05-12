"""Add managed billing: plan templates, plan assignments, FX policy.

Adds two new tables (``billing_plan_template``, ``billing_plan_assignment``)
and extends ``billing_account``, ``recharge``, and ``credit_transaction`` with
columns to support the plan-driven billing path described in the v2 spec.

Schema highlights:

* ``billing_plan_template`` — immutable, named billing configurations.
  Catalog placement is described by two orthogonal booleans
  (``is_custom``, ``is_active``) instead of a tri-state availability enum
  so we can express "deprecated bespoke" and "deprecated catalog"
  separately. Seeded with a single ``default`` row (id=1)
  representing the platform-default plan that every account is
  assigned to unless an operator explicitly assigns a custom contract.
  Plan-type ("PAYG" vs "COMMITMENT") is *derived* from
  ``commit_amount`` (NULL/zero = PAYG, positive = COMMITMENT) — there
  is no separate enum column. Multi-currency contracts carry an
  ``fx_policy`` (LOCKED_RATE / SPOT / PERIOD_AVERAGE) that decides how
  USD ledger usage is converted into the contract's ``currency`` at
  invoice time; ``fx_policy`` is NULL for USD templates (no conversion
  needed). Live-fetch policies (SPOT / PERIOD_AVERAGE) hit Frankfurter
  on the spot and pin the resolved rate into ``Recharge.detail`` for
  re-run determinism — no daily snapshot table, no FX cron.

* ``billing_plan_assignment`` — time-bounded assignments of templates to
  accounts. **Every account always has at least one row** (pristine
  accounts get a single open row pointing at the seeded default
  template). A unique partial index ensures at most one active
  assignment per account. History is reconstructed by ``started_at
  DESC`` order; there is intentionally no per-row ``supersedes``
  pointer (it would duplicate time order).

* ``billing_account.plan_assignment_id`` — pointer to the currently-active
  assignment row. **Nullable in the DB but NOT NULL by application
  contract**: every account has a real pointer to a real row, with
  pristine accounts pointing at the backfilled default plan assignment.
  The column is left nullable because PostgreSQL ``NOT NULL`` is not
  deferrable and we'd otherwise hit a chicken-and-egg at row creation
  (BA → assignment → BA). Instead the invariant is enforced by:
  (1) ``BillingAccountDAO.create`` always inserts the default
  assignment in the same flush; (2) this migration backfills every
  existing row; (3) the daily reconciliation routine flags any NULL as
  ``critical`` (``plan_assignment_null_pointer``). A ``set_plan`` call
  closes the active row and inserts a new one (including for
  cancellations, which insert a fresh default plan assignment).

* ``recharge.plan_id`` + ``recharge.detail`` — link each invoice back to the
  exact plan version that produced it, plus an audit JSONB of the formula
  inputs (incl. resolved FX rate + provenance for non-USD contracts).
  Both NULL for existing rows (backfill of historical recharges out of
  scope; only forward Recharges carry plan attribution).

* ``credit_transaction.plan_assignment_id`` — denormalises the
  plan in force when each ledger row was written. Forward-compat for
  PRORATE_IMMEDIATELY plan changes; NULL on historical rows.

``billing_mode`` is *not* denormalised on the account row — callers use
``BillingAccountDAO.resolve_billing_mode()`` which is a single indexed
join to the assignment's template.

Revision ID: metered_and_billing_plan
Revises: contact_cm_authoring
Create Date: 2026-05-08 14:30:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "metered_and_billing_plan"
# Re-parented onto the upstream ``contact_cm_authoring`` head (added on
# 2026-05-06) when this branch was rebased over staging on 2026-05-08.
# The original parent was ``tighten_space_description`` — both the
# upstream contact-membership migration and this one chained off it,
# producing a transient two-head DAG. Linearising the chain (instead of
# adding a merge revision) keeps ``alembic upgrade head`` deterministic
# in CI and matches the project's convention of single-head history.
down_revision = "contact_cm_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. billing_plan_template
    # ------------------------------------------------------------------
    op.create_table(
        "billing_plan_template",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        # Customer-facing label for invoice line items / dashboard summaries.
        # Nullable; the application falls back to ``name`` when absent so
        # templates with no explicit customer label render fine.
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # Settlement model — orthogonal to commit shape. Plan-type
        # ("PAYG" vs "COMMITMENT") is derived from commit_amount; no
        # separate enum column.
        sa.Column(
            "billing_mode",
            sa.String(),
            server_default="CREDITS",
            nullable=False,
        ),
        # Commit shape — NULL/zero commit_amount = pay-as-you-go.
        # ``currency`` is the invoice currency for the whole template
        # (including PAYG, where there's no commit but the customer is
        # still invoiced in this currency).
        sa.Column("commit_amount", sa.Numeric(), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column("commit_period", sa.String(), nullable=True),
        sa.Column("commit_schedule", sa.String(), nullable=True),
        # Pricing — two stacked multipliers. ``base_pricing_factor``
        # applies to ALL usage (commit-included + overage + PAYG);
        # ``overage_pricing_factor`` is an ADDITIONAL multiplier on
        # top of base, only for the overage portion. Effective
        # above-commit rate = base × overage. Defaults of 1.0/1.0
        # reproduce list-price behaviour with no overage penalty.
        # There is intentionally no overage_policy / monthly_usage_cap;
        # the platform never blocks usage based on plan terms —
        # over-consumption just bills at the (base × overage) rate.
        sa.Column(
            "base_pricing_factor",
            sa.Numeric(),
            server_default="1.0",
            nullable=False,
        ),
        sa.Column(
            "overage_pricing_factor",
            sa.Numeric(),
            server_default="1.0",
            nullable=False,
        ),
        # Collection
        sa.Column(
            "collection_method",
            sa.String(),
            server_default="AUTO_CARD",
            nullable=False,
        ),
        # Lifecycle behaviours
        sa.Column(
            "proration_policy",
            sa.String(),
            server_default="PRORATE",
            nullable=False,
        ),
        # Unused-credits behaviour at period-end. Only meaningful for
        # COMMITMENT+CREDITS plans; check constraint enforces NULL
        # everywhere else.
        sa.Column("credits_rollover_policy", sa.String(), nullable=True),
        # FX policy — how USD ledger usage is converted to ``currency``
        # at invoice time. NULL for USD templates (no conversion);
        # LOCKED_RATE / SPOT / PERIOD_AVERAGE for non-USD templates.
        # SPOT / PERIOD_AVERAGE live-fetch from Frankfurter and pin the
        # resolved rate into Recharge.detail for re-run determinism.
        sa.Column("fx_policy", sa.String(length=32), nullable=True),
        sa.Column("fx_locked_rate", sa.Numeric(18, 8), nullable=True),
        # Catalog placement — two orthogonal booleans (replaces the
        # legacy tri-state ``availability`` enum). ``is_custom`` =
        # bespoke per-customer contract (hidden from public catalog).
        # ``is_active`` = accepting new assignments. Deprecating a plan
        # flips ``is_active`` to false; the ``is_custom`` flag is
        # preserved.
        sa.Column(
            "is_custom",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("supersedes_template_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="ux_billing_plan_template_name"),
        sa.ForeignKeyConstraint(
            ["supersedes_template_id"],
            ["billing_plan_template.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
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
            "commit_schedule IS NULL OR commit_schedule IN "
            "('AMORTISED', 'UPFRONT')",
            name="ck_plan_template_commit_schedule",
        ),
        sa.CheckConstraint(
            "collection_method IN ('AUTO_CARD', 'SEND_INVOICE_NET_30')",
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
        # Pricing factors must be strictly positive — zero would
        # silently waive every charge, which is never the intent (use
        # is_active=false to retire a plan).
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
        # UPFRONT-schedule plans bill the full commit on
        # anniversaries; prorating that lump sum across a mid-month
        # start would confuse customer + accounting. Require
        # FULL_FIRST so the first invoice carries the full commit
        # and anniversaries land on round month boundaries.
        sa.CheckConstraint(
            "commit_schedule IS DISTINCT FROM 'UPFRONT' OR "
            "proration_policy = 'FULL_FIRST'",
            name="ck_plan_template_upfront_requires_full_first",
        ),
        # PERIOD_AVERAGE FX is incompatible with UPFRONT — the
        # "average over the billing period" concept is ambiguous when
        # the period is the contract period rather than a calendar
        # month. Allow LOCKED_RATE or SPOT only for UPFRONT non-USD.
        sa.CheckConstraint(
            "commit_schedule IS DISTINCT FROM 'UPFRONT' OR "
            "fx_policy IS NULL OR fx_policy IN ('LOCKED_RATE', 'SPOT')",
            name="ck_plan_template_upfront_no_period_average_fx",
        ),
        # Credits-rollover policy is COMMITMENT+CREDITS only.
        sa.CheckConstraint(
            "credits_rollover_policy IS NULL OR "
            "(commit_amount IS NOT NULL AND commit_amount > 0 "
            "AND billing_mode = 'CREDITS')",
            name="ck_plan_template_credits_rollover_scope",
        ),
        sa.CheckConstraint(
            "fx_policy IS NULL OR "
            "fx_policy IN ('LOCKED_RATE', 'SPOT', 'PERIOD_AVERAGE')",
            name="ck_plan_template_fx_policy",
        ),
        # USD ⇔ no-FX, non-USD ⇔ FX policy required.
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
    )
    op.create_index(
        "ix_plan_template_is_active",
        "billing_plan_template",
        ["is_active"],
    )

    # ------------------------------------------------------------------
    # 2. billing_plan_assignment
    # ------------------------------------------------------------------
    op.create_table(
        "billing_plan_assignment",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            ["billing_account.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["billing_plan_template.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_billing_plan_assignment_window",
        ),
    )
    op.create_index(
        "ix_billing_plan_assignment_billing_account_id",
        "billing_plan_assignment",
        ["billing_account_id"],
    )
    op.create_index(
        "ix_billing_plan_assignment_template_id",
        "billing_plan_assignment",
        ["template_id"],
    )
    op.create_index(
        "ix_billing_plan_assignment_account_started",
        "billing_plan_assignment",
        ["billing_account_id", "started_at"],
    )
    # At most one currently-active assignment per billing account.
    op.create_index(
        "ux_billing_plan_assignment_active_unique",
        "billing_plan_assignment",
        ["billing_account_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # 3. Seed the platform-default template at id=1.
    # Every account is assigned this template at signup (via the v2
    # account-creation flow) or via the backfill below for accounts
    # that pre-date v2.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO billing_plan_template (
            id, name, display_name, description,
            billing_mode,
            commit_amount, currency, commit_period, commit_schedule,
            base_pricing_factor, overage_pricing_factor,
            collection_method,
            proration_policy, credits_rollover_policy,
            fx_policy, fx_locked_rate,
            is_custom, is_active, created_at
        ) VALUES (
            1, 'default', 'Default',
            'Platform-default pay-as-you-go plan. Credit-based wallet with '
            'auto-recharge support. Every account is assigned this template '
            'at signup (or backfilled to it for accounts that pre-date v2).',
            'CREDITS',
            NULL, 'USD', NULL, NULL,
            1.0, 1.0,
            'AUTO_CARD',
            'PRORATE', NULL,
            NULL, NULL,
            false, true, now()
        )
        ON CONFLICT (id) DO NOTHING
        """,
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('billing_plan_template', 'id'), "
        "GREATEST(1, (SELECT COALESCE(MAX(id), 1) FROM billing_plan_template)))",
    )

    # ------------------------------------------------------------------
    # 4. Extend billing_account with plan_assignment_id and backfill.
    #
    # The column is left nullable in the DB even though the application
    # contract is "always non-null" (every account has a default
    # assignment from creation time onwards). The reason is that
    # PostgreSQL NOT NULL is not deferrable, and creating a brand-new
    # account would otherwise hit a chicken-and-egg: the BA INSERT
    # would need a real assignment id, but the assignment INSERT needs
    # the BA id (assignment.billing_account_id is NOT NULL too).
    #
    # The invariant is enforced via:
    #   * `BillingAccountDAO.create` always inserts the default
    #     assignment in the same flush window;
    #   * the backfill below covers every existing row;
    #   * the reconciliation routine flags any NULL pointer as
    #     `plan_assignment_null_pointer` (critical).
    # ------------------------------------------------------------------
    op.add_column(
        "billing_account",
        sa.Column("plan_assignment_id", sa.BigInteger(), nullable=True),
    )

    # Backfill: insert one default plan assignment per existing account
    # and point the account at it. CTE captures the new ids in a single
    # statement so we don't need a Python loop.
    op.execute(
        """
        WITH new_assignments AS (
            INSERT INTO billing_plan_assignment (
                billing_account_id, template_id, started_at, change_reason
            )
            SELECT
                id, 1, now(),
                'default (backfilled by metered_and_billing_plan)'
            FROM billing_account
            WHERE plan_assignment_id IS NULL
            RETURNING id, billing_account_id
        )
        UPDATE billing_account
        SET plan_assignment_id = na.id
        FROM new_assignments na
        WHERE billing_account.id = na.billing_account_id
        """,
    )

    op.create_foreign_key(
        "fk_billing_account_plan_assignment",
        "billing_account",
        "billing_plan_assignment",
        ["plan_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_billing_account_plan_assignment_id",
        "billing_account",
        ["plan_assignment_id"],
    )

    # ------------------------------------------------------------------
    # 4b. Per-customer payment-method preference.
    #
    # Stored on the BA (not the template) because it's a property of how
    # *this customer* prefers to settle, not a contract term — same
    # template can serve a wire-paying enterprise and a card-paying
    # one. NULL means "use the invoicer's defaults":
    # ``['card']`` for AUTO_CARD invoices, ``['card',
    # 'customer_balance']`` for SEND_INVOICE_NET_30 once
    # ``customer_balance`` is enabled in the Stripe dashboard.
    # ------------------------------------------------------------------
    op.add_column(
        "billing_account",
        sa.Column(
            "preferred_payment_method_types",
            sa.dialects.postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # 5. Extend recharge with plan_id + detail.
    # ------------------------------------------------------------------
    op.add_column(
        "recharge",
        sa.Column("plan_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "recharge",
        sa.Column(
            "detail",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_recharge_plan",
        "recharge",
        "billing_plan_assignment",
        ["plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_recharge_plan_id", "recharge", ["plan_id"])

    # ------------------------------------------------------------------
    # 6. credit_transaction: add plan_assignment_id, drop balance_after.
    #
    # ``balance_after`` was a CREDITS-mode wallet snapshot. It was unused
    # by any consumer (no reconciliation routine, no UI) and is mode-
    # specific in a ledger that should be billing-mode-agnostic. Drift
    # detection, if needed, can sum signed amounts and compare with
    # ``billing_account.credits``.
    # ------------------------------------------------------------------
    op.add_column(
        "credit_transaction",
        sa.Column(
            "plan_assignment_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_credit_txn_plan_assignment",
        "credit_transaction",
        "billing_plan_assignment",
        ["plan_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_credit_txn_plan_assignment",
        "credit_transaction",
        ["plan_assignment_id"],
    )
    op.drop_column("credit_transaction", "balance_after")

    # ------------------------------------------------------------------
    # 7. Plan groups — curated bundles of switchable templates.
    #
    # ``plan_group`` rows are persistence-only objects an operator
    # creates to declare a set of templates that an account can switch
    # between via the customer-facing self-serve endpoint. Templates
    # themselves are unchanged — a template can be a member of zero,
    # one, or many groups (a 5k-tier template might appear in the
    # public Vantage ladder and in a bespoke combined ladder for a
    # specific customer who has additional tiers).
    #
    # The ``plan_group_member.position`` column does double duty:
    # NULL means "this group is an unordered set of alternatives"
    # (rendered as side-by-side cards client-side); a populated value
    # means "this group is an ordered ladder" (rendered as up/down
    # rungs). When set, positions must be unique within a group so the
    # ladder ordering is well-defined; the partial unique index below
    # enforces that without forcing every group to be a ladder.
    #
    # ``BillingAccount.plan_group_id`` is the per-account assignment
    # — single FK because every account is on at most one ladder/offer
    # set at a time (mirrors the single-FK pattern used for
    # ``plan_assignment_id``). NULL means "no self-serve switching for
    # this account; admins can still call set_plan() directly".
    # Downgrade direction is determined by member.position (target <
    # current = downgrade) which combines with the AT_BOUNDARY policy
    # so customers can move freely up the ladder but downgrades are
    # always deferred to next period.
    # ------------------------------------------------------------------
    op.create_table(
        "plan_group",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="ux_plan_group_name"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_plan_group_is_active", "plan_group", ["is_active"])

    op.create_table(
        "plan_group_member",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        # NULL = unordered offer (cards UX); set = ordered ladder rung
        # (lower position = "smaller" tier = downgrade target).
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column(
            "added_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("group_id", "template_id"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["plan_group.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["billing_plan_template.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "position IS NULL OR position >= 0",
            name="ck_plan_group_member_position_non_negative",
        ),
    )
    # Positions must be unique within a group when set — so a ladder
    # has well-defined ordering. NULL positions are allowed in
    # unlimited number (unordered groups).
    op.create_index(
        "ux_plan_group_member_position",
        "plan_group_member",
        ["group_id", "position"],
        unique=True,
        postgresql_where=sa.text("position IS NOT NULL"),
    )
    op.create_index(
        "ix_plan_group_member_template_id",
        "plan_group_member",
        ["template_id"],
    )

    # ------------------------------------------------------------------
    # 7b. Seed the platform-default plan_group at id=1.
    #
    # Mirrors the DEFAULT_TEMPLATE_ID = 1 sentinel pattern so callers
    # that reach for "the default" — at signup-time auto-assignment
    # (via the BillingAccount column default below), in tests, and in
    # the admin UI — only ever need to know one constant. The default
    # group starts with the default template as its single member;
    # the FE hide-rule suppresses the switcher UX whenever the only
    # entry is the customer's current plan, so a "group of one" is
    # invisible to customers until a second template (e.g. a paid
    # tier) joins it.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO plan_group (
            id, name, display_name, description,
            is_active, created_at
        ) VALUES (
            1, 'default', 'Default',
            'Platform-default plan group, auto-assigned to every '
            'account. Today contains only the default template — the '
            'switcher UI hides itself in that case. Add a paid tier '
            'here to expose self-serve upgrades to every account.',
            true, now()
        )
        ON CONFLICT (id) DO NOTHING
        """,
    )
    op.execute(
        """
        INSERT INTO plan_group_member (group_id, template_id, position, added_at)
        VALUES (1, 1, 0, now())
        ON CONFLICT (group_id, template_id) DO NOTHING
        """,
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('plan_group', 'id'), "
        "GREATEST(1, (SELECT COALESCE(MAX(id), 1) FROM plan_group)))",
    )

    # Per-account assignment to one group (the set of plans this
    # account can self-serve switch between).
    #
    # ``server_default = '1'`` so brand-new accounts inherit the
    # platform-default group automatically at INSERT time, mirroring
    # the convention used for ``plan_assignment_id`` (every account
    # always points at *some* assignment / *some* group). The column
    # is added nullable, then backfilled to 1 for any historical row,
    # then promoted to ``NOT NULL`` so opt-out is no longer a valid
    # state — operators that want a customer to have no self-serve
    # switching simply leave them on the platform-default group, where
    # the customer's current (off-catalog) template won't appear in
    # the group's members and the FE hide-rule suppresses the
    # switcher.
    #
    # ``ON DELETE RESTRICT`` (rather than SET NULL) on the FK
    # mirrors the same invariant: deleting a group that an account
    # still references would otherwise silently NULL the column,
    # re-introducing the very state we're forbidding.
    op.add_column(
        "billing_account",
        sa.Column(
            "plan_group_id",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_billing_account_plan_group",
        "billing_account",
        "plan_group",
        ["plan_group_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_billing_account_plan_group_id",
        "billing_account",
        ["plan_group_id"],
    )

    # Backfill every historical row to the default group, then
    # promote the column to NOT NULL. The backfill must run
    # *before* the constraint flip, otherwise the ALTER would
    # reject any pre-existing NULL row.
    op.execute(
        """
        UPDATE billing_account
        SET plan_group_id = 1
        WHERE plan_group_id IS NULL
        """,
    )
    op.alter_column(
        "billing_account",
        "plan_group_id",
        nullable=False,
        existing_type=sa.BigInteger(),
        existing_server_default=sa.text("1"),
    )


def downgrade() -> None:
    # plan_group plumbing (drop in reverse dependency order)
    op.drop_index(
        "ix_billing_account_plan_group_id",
        table_name="billing_account",
    )
    op.drop_constraint(
        "fk_billing_account_plan_group",
        "billing_account",
        type_="foreignkey",
    )
    op.drop_column("billing_account", "plan_group_id")
    op.drop_index(
        "ix_plan_group_member_template_id",
        table_name="plan_group_member",
    )
    op.drop_index(
        "ux_plan_group_member_position",
        table_name="plan_group_member",
    )
    op.drop_table("plan_group_member")
    op.drop_index("ix_plan_group_is_active", table_name="plan_group")
    op.drop_table("plan_group")

    # credit_transaction
    op.add_column(
        "credit_transaction",
        sa.Column("balance_after", sa.Numeric(), nullable=True),
    )
    op.drop_index("ix_credit_txn_plan_assignment", table_name="credit_transaction")
    op.drop_constraint(
        "fk_credit_txn_plan_assignment",
        "credit_transaction",
        type_="foreignkey",
    )
    op.drop_column("credit_transaction", "plan_assignment_id")

    # recharge
    op.drop_index("ix_recharge_plan_id", table_name="recharge")
    op.drop_constraint("fk_recharge_plan", "recharge", type_="foreignkey")
    op.drop_column("recharge", "detail")
    op.drop_column("recharge", "plan_id")

    # billing_account
    op.drop_column("billing_account", "preferred_payment_method_types")
    op.drop_index(
        "ix_billing_account_plan_assignment_id",
        table_name="billing_account",
    )
    op.drop_constraint(
        "fk_billing_account_plan_assignment",
        "billing_account",
        type_="foreignkey",
    )
    op.drop_column("billing_account", "plan_assignment_id")

    # billing_plan_assignment
    op.drop_index(
        "ux_billing_plan_assignment_active_unique",
        table_name="billing_plan_assignment",
    )
    op.drop_index(
        "ix_billing_plan_assignment_account_started",
        table_name="billing_plan_assignment",
    )
    op.drop_index(
        "ix_billing_plan_assignment_template_id",
        table_name="billing_plan_assignment",
    )
    op.drop_index(
        "ix_billing_plan_assignment_billing_account_id",
        table_name="billing_plan_assignment",
    )
    op.drop_table("billing_plan_assignment")

    # billing_plan_template
    op.drop_index(
        "ix_plan_template_is_active",
        table_name="billing_plan_template",
    )
    op.drop_table("billing_plan_template")
