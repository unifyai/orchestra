"""Self-serve subscription billing model.

The self-serve, Stripe-subscription credit model, grouped into one
migration because these pieces ship together and share the tier catalog:

1. Relax ``ck_plan_template_collection_method`` to allow
   ``collection_method = 'STRIPE_SUBSCRIPTION'``.
2. Add the subscription columns to ``billing_account``
   (``stripe_subscription_id``, ``auto_increment``, ``current_period_end``,
   ``plan_credits_granted_period``) + the subscription-id index.
3. Restore the platform-bootstrap singletons (default plan group id=1,
   default pay-as-you-go template id=1) so a freshly-migrated DB can seed.
4. Seed the 21 monthly self-serve tier templates (positions 1..21) and add
   them — plus the default template at position 0 — to the default group.
5. Backfill ``plan_credits_granted_period`` for accounts already on a live
   subscription tier (so a first post-migration upgrade grants only the
   delta).
6. Drop the dead auto-recharge columns (``autorecharge``,
   ``autorecharge_threshold``, ``autorecharge_qty``).
7. Seed the 21 annual tier templates (positions 101..121) into the same
   default group on a distinct position band.
8. Add the credit-expiry reminder idempotency stamp
   (``credit_expiry_reminded_at``).
9. Add ``subscription_cancel_at_period_end`` (mirrors Stripe's flag).
10. Add ``payment_past_due_at`` — the soft-dunning delinquency marker that
    keeps ``suspension_reason`` meaning "why suspended" (NULL while ACTIVE).

At ``1 credit = $1`` the monthly USD price equals the monthly credit grant,
so each template's ``commit_amount`` is its price (== grant == the Stripe
subscription quantity). Annual tiers share the monthly *rung*
(``commit_amount``) but bill 12× and grant the whole year up front.

This chains onto ``2026_retire_spaces`` (the staging head as of the merge);
that revision already has both ``seed_personal_cm`` and
``2026_plot_table_context_fks`` in its ancestry, so a single linear parent
keeps the graph at one head.

Revision ID: self_serve_billing
Revises: 2026_retire_spaces
Create Date: 2026-06-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "self_serve_billing"
down_revision = "2026_retire_spaces"
branch_labels = None
depends_on = None


# Default plan group / default template sentinels (NEVER renumber).
_DEFAULT_PLAN_GROUP_ID = 1
_DEFAULT_TEMPLATE_ID = 1
_ANNUAL_MONTHS = 12

# (monthly USD price == credit grant == subscription quantity, ladder
#  position). Positions ascend with price; the default template stays at
#  position 0. The annual ladder sits on a 100-band so it never collides
#  with the monthly rungs (1..21) inside the same group.
_MONTHLY_TIERS: tuple[tuple[int, int], ...] = (
    (50, 1),
    (75, 2),
    (100, 3),
    (200, 4),
    (300, 5),
    (400, 6),
    (500, 7),
    (750, 8),
    (1000, 9),
    (1500, 10),
    (2000, 11),
    (3000, 12),
    (4000, 13),
    (5000, 14),
    (7500, 15),
    (10000, 16),
    (12500, 17),
    (15000, 18),
    (20000, 19),
    (25000, 20),
    (30000, 21),
)

_ANNUAL_TIERS: tuple[tuple[int, int], ...] = tuple(
    (price, pos + 100) for price, pos in _MONTHLY_TIERS
)


def _monthly_template_rows_sql() -> str:
    rows = []
    for price, _pos in _MONTHLY_TIERS:
        name = f"tier_{price}"
        display = f"${price:,} / mo"
        description = (
            f"{price:,} credits / month self-serve plan "
            f"(${price:,}/mo at 1 credit = $1)."
        )
        # All values are integers or controlled literals — no injection risk.
        rows.append(
            "("
            f"'{name}', '{display}', '{description}', "
            "'CREDITS', "
            f"{price}, 'USD', 'MONTHLY', 'AMORTISED', "
            "1.0, 1.0, "
            "'STRIPE_SUBSCRIPTION', "
            "'PRORATE', 'FORFEIT_AT_PERIOD_END', "
            "NULL, NULL, "
            "false, true"
            ")"
        )
    return ",\n            ".join(rows)


def _annual_template_rows_sql() -> str:
    rows = []
    for price, _pos in _ANNUAL_TIERS:
        name = f"tier_{price}_annual"
        annual_price = price * _ANNUAL_MONTHS
        annual_credits = price * _ANNUAL_MONTHS
        display = f"${annual_price:,} / yr"
        description = (
            f"{annual_credits:,} credits / year self-serve plan "
            f"(${annual_price:,}/yr at 1 credit = $1, billed annually)."
        )
        rows.append(
            "("
            f"'{name}', '{display}', '{description}', "
            "'CREDITS', "
            f"{price}, 'USD', 'ANNUAL', 'AMORTISED', "
            "1.0, 1.0, "
            "'STRIPE_SUBSCRIPTION', "
            "'PRORATE', 'FORFEIT_AT_PERIOD_END', "
            "NULL, NULL, "
            "false, true"
            ")"
        )
    return ",\n            ".join(rows)


def _member_rows_sql(tiers: tuple[tuple[int, int], ...], suffix: str = "") -> str:
    rows = []
    for price, pos in tiers:
        rows.append(f"('tier_{price}{suffix}', {pos})")
    return ",\n            ".join(rows)


def upgrade() -> None:
    # 1. Relax the collection_method CHECK to allow STRIPE_SUBSCRIPTION.
    op.execute(
        """
        ALTER TABLE billing_plan_template
            DROP CONSTRAINT IF EXISTS ck_plan_template_collection_method;
        ALTER TABLE billing_plan_template
            ADD CONSTRAINT ck_plan_template_collection_method
            CHECK (collection_method IN (
                'AUTO_CARD', 'SEND_INVOICE_NET_30', 'STRIPE_SUBSCRIPTION'
            ));
        """,
    )

    # 2. New billing_account subscription columns.
    op.execute(
        """
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS stripe_subscription_id varchar;
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS auto_increment boolean NOT NULL
            DEFAULT false;
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS current_period_end timestamptz;
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS plan_credits_granted_period numeric
            NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS ix_billing_account_stripe_subscription_id
            ON billing_account (stripe_subscription_id);
        """,
    )

    # 3. Restore the platform-bootstrap singletons that the squashed
    #    ``_platform_initial`` schema dump dropped (it is schema-only): the
    #    default plan group (id=1 — the NOT NULL default for
    #    ``billing_account.plan_group_id`` with a RESTRICT FK) and the default
    #    pay-as-you-go template (id=1 — what every account resolves to with no
    #    live paid plan). In production these are created by platform
    #    bootstrap; on a freshly-migrated DB (CI, a new/wiped local DB) they
    #    don't exist yet, and seeding runs *after* migrations. All idempotent;
    #    mirrors ``orchestra/tests/seeding.sql``.
    op.execute(
        f"""
        INSERT INTO plan_group (id, name, display_name, description, is_active)
        VALUES ({_DEFAULT_PLAN_GROUP_ID}, 'default', 'Default',
                'Platform-default plan group, auto-assigned to every account.',
                true)
        ON CONFLICT DO NOTHING;
        """,
    )
    op.execute(
        "SELECT setval('plan_group_id_seq', "
        "GREATEST((SELECT MAX(id) FROM plan_group), 1));"
    )
    op.execute(
        f"""
        INSERT INTO billing_plan_template (
            id, name, display_name, description,
            billing_mode,
            commit_amount, currency, commit_period, commit_schedule,
            base_pricing_factor, overage_pricing_factor,
            collection_method,
            proration_policy, credits_rollover_policy,
            fx_policy, fx_locked_rate,
            is_custom, is_active
        )
        VALUES (
            {_DEFAULT_TEMPLATE_ID}, 'default', 'Default',
            'Platform-default pay-as-you-go plan. Credit-based wallet.',
            'CREDITS',
            NULL, 'USD', NULL, NULL,
            1.0, 1.0,
            'AUTO_CARD',
            'PRORATE', NULL,
            NULL, NULL,
            false, true
        )
        ON CONFLICT DO NOTHING;
        """,
    )
    # Advance the template id sequence past the explicit id=1 so the tier
    # inserts below (which let the sequence assign ids) don't collide with it.
    op.execute(
        "SELECT setval('billing_plan_template_id_seq', "
        "GREATEST((SELECT MAX(id) FROM billing_plan_template), 1));"
    )

    # 4a. Seed the 21 monthly tier templates (idempotent on the unique name).
    op.execute(
        f"""
        INSERT INTO billing_plan_template (
            name, display_name, description,
            billing_mode,
            commit_amount, currency, commit_period, commit_schedule,
            base_pricing_factor, overage_pricing_factor,
            collection_method,
            proration_policy, credits_rollover_policy,
            fx_policy, fx_locked_rate,
            is_custom, is_active
        )
        VALUES
            {_monthly_template_rows_sql()}
        ON CONFLICT (name) DO NOTHING;
        """,
    )

    # 4b. Add each monthly tier as an ascending-position member of the default
    #     plan group (group 1).
    op.execute(
        f"""
        INSERT INTO plan_group_member (group_id, template_id, position)
        SELECT {_DEFAULT_PLAN_GROUP_ID}, t.id, v.position
        FROM (VALUES
            {_member_rows_sql(_MONTHLY_TIERS)}
        ) AS v(name, position)
        JOIN billing_plan_template t ON t.name = v.name
        ON CONFLICT (group_id, template_id) DO NOTHING;
        """,
    )

    # 4c. Link the default template into the default group at position 0 (its
    #     free/base rung), mirroring seeding.sql.
    op.execute(
        f"""
        INSERT INTO plan_group_member (group_id, template_id, position)
        VALUES ({_DEFAULT_PLAN_GROUP_ID}, {_DEFAULT_TEMPLATE_ID}, 0)
        ON CONFLICT (group_id, template_id) DO NOTHING;
        """,
    )

    # 5. Backfill the per-period upgrade high-water-mark for any account
    #    already on a live subscription tier, so a first post-migration
    #    upgrade grants only the delta (not the full new tier).
    op.execute(
        """
        UPDATE billing_account ba
        SET plan_credits_granted_period = t.commit_amount
        FROM billing_plan_assignment pa
        JOIN billing_plan_template t ON t.id = pa.template_id
        WHERE ba.plan_assignment_id = pa.id
          AND ba.stripe_subscription_id IS NOT NULL
          AND t.collection_method = 'STRIPE_SUBSCRIPTION';
        """,
    )

    # 6. Drop the dead auto-recharge columns (retired with the prepaid-wallet
    #    auto-recharge feature; no longer read or written).
    op.execute(
        """
        ALTER TABLE billing_account DROP COLUMN IF EXISTS autorecharge;
        ALTER TABLE billing_account DROP COLUMN IF EXISTS autorecharge_threshold;
        ALTER TABLE billing_account DROP COLUMN IF EXISTS autorecharge_qty;
        """,
    )

    # 7a. Seed the 21 annual tier templates (parallel to the monthly ladder).
    op.execute(
        f"""
        INSERT INTO billing_plan_template (
            name, display_name, description,
            billing_mode,
            commit_amount, currency, commit_period, commit_schedule,
            base_pricing_factor, overage_pricing_factor,
            collection_method,
            proration_policy, credits_rollover_policy,
            fx_policy, fx_locked_rate,
            is_custom, is_active
        )
        VALUES
            {_annual_template_rows_sql()}
        ON CONFLICT (name) DO NOTHING;
        """,
    )

    # 7b. Add each annual tier on the 100-band so the interval-aware ladder
    #     orders them independently of the monthly rungs without collisions.
    op.execute(
        f"""
        INSERT INTO plan_group_member (group_id, template_id, position)
        SELECT {_DEFAULT_PLAN_GROUP_ID}, t.id, v.position
        FROM (VALUES
            {_member_rows_sql(_ANNUAL_TIERS, suffix="_annual")}
        ) AS v(name, position)
        JOIN billing_plan_template t ON t.name = v.name
        ON CONFLICT (group_id, template_id) DO NOTHING;
        """,
    )

    # 8. Add the credit-expiry reminder idempotency stamp. Nullable, no
    #    default: existing accounts start un-reminded.
    op.add_column(
        "billing_account",
        sa.Column(
            "credit_expiry_reminded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    # 9. Mirror Stripe's ``cancel_at_period_end`` for a persistent
    #    "cancels on X" indicator.
    op.add_column(
        "billing_account",
        sa.Column(
            "subscription_cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # 10. Soft-dunning delinquency marker. Nullable, no default: existing
    #     accounts start not-past-due. Lets ``suspension_reason`` stay NULL
    #     while an account is ACTIVE-but-delinquent during Stripe's retries.
    op.add_column(
        "billing_account",
        sa.Column(
            "payment_past_due_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Reverse of ``upgrade`` (newest step first).

    # 10. Drop the soft-dunning delinquency marker.
    op.drop_column("billing_account", "payment_past_due_at")

    # 9. Drop the cancel-at-period-end mirror.
    op.drop_column("billing_account", "subscription_cancel_at_period_end")

    # 8. Drop the credit-expiry reminder stamp.
    op.drop_column("billing_account", "credit_expiry_reminded_at")

    # 7. Remove the annual tier templates + memberships.
    annual_names = ", ".join(f"'tier_{price}_annual'" for price, _p in _ANNUAL_TIERS)
    op.execute(
        f"""
        DELETE FROM plan_group_member
        WHERE template_id IN (
            SELECT id FROM billing_plan_template WHERE name IN ({annual_names})
        );
        DELETE FROM billing_plan_template WHERE name IN ({annual_names});
        """,
    )

    # 6. Re-add the auto-recharge columns with their original server defaults.
    op.execute(
        """
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS autorecharge boolean NOT NULL
            DEFAULT false;
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS autorecharge_threshold numeric NOT NULL
            DEFAULT 0;
        ALTER TABLE billing_account
            ADD COLUMN IF NOT EXISTS autorecharge_qty numeric NOT NULL
            DEFAULT 25;
        """,
    )

    # 4/5. Remove the monthly tier templates + memberships (the bootstrap
    #      singletons and the position-0 default member are intentionally left
    #      in place, matching the original incremental downgrades).
    monthly_names = ", ".join(f"'tier_{price}'" for price, _p in _MONTHLY_TIERS)
    op.execute(
        f"""
        DELETE FROM plan_group_member
        WHERE template_id IN (
            SELECT id FROM billing_plan_template WHERE name IN ({monthly_names})
        );
        DELETE FROM billing_plan_template WHERE name IN ({monthly_names});
        """,
    )

    # 2. Drop the subscription columns + index.
    op.execute(
        """
        DROP INDEX IF EXISTS ix_billing_account_stripe_subscription_id;
        ALTER TABLE billing_account
            DROP COLUMN IF EXISTS plan_credits_granted_period;
        ALTER TABLE billing_account DROP COLUMN IF EXISTS current_period_end;
        ALTER TABLE billing_account DROP COLUMN IF EXISTS auto_increment;
        ALTER TABLE billing_account DROP COLUMN IF EXISTS stripe_subscription_id;
        """,
    )

    # 1. Restore the original (narrower) collection_method CHECK.
    op.execute(
        """
        ALTER TABLE billing_plan_template
            DROP CONSTRAINT IF EXISTS ck_plan_template_collection_method;
        ALTER TABLE billing_plan_template
            ADD CONSTRAINT ck_plan_template_collection_method
            CHECK (collection_method IN ('AUTO_CARD', 'SEND_INVOICE_NET_30'));
        """,
    )
