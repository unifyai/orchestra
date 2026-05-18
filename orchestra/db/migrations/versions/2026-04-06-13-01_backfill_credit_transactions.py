"""Backfill credit_transaction from historical data.

Revision ID: backfill_credit_ledger
Revises: add_credit_ledger
Create Date: 2026-04-06 13:01:00.000000

This migration populates the credit_transaction table with historical data
from existing tables:

1. Recharges (PAID status) → positive credit transactions
2. Assistant hiring → inferred from assistants.created_at × fixed cost
3. Contact setup fees → inferred from assistant_contacts × contact_type_costs
4. Contact levies → inferred from last_billed_month on assistant_contacts
5. LLM costs → from LogEvent rows in the Assistants project (Events/LLM)

balance_after is left NULL for historical rows since we cannot reliably
reconstruct the running balance from interleaved concurrent transactions.
"""

import sqlalchemy as sa
from alembic import op

revision = "backfill_credit_ledger"
down_revision = "add_credit_ledger"
branch_labels = None
depends_on = None

ASSISTANT_CREATION_COST = 10.0


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Backfill from recharges (PAID only)
    conn.execute(
        sa.text(
            """
            INSERT INTO credit_transaction
                (billing_account_id, at, amount, balance_after, category, description, detail)
            SELECT
                r.billing_account_id,
                r.at,
                r.quantity,
                NULL,
                CASE
                    WHEN r.type = 'promo' THEN 'promo'
                    ELSE 'recharge'
                END,
                CASE
                    WHEN r.type = 'promo' THEN 'Promo credit grant'
                    WHEN r.type = 'auto' THEN 'Auto-recharge'
                    ELSE 'Payment recharge'
                END,
                jsonb_build_object(
                    'event', 'backfill_recharge',
                    'recharge_id', r.id,
                    'type', r.type,
                    'stripe_invoice_id', r.stripe_invoice_id
                )
            FROM recharge r
            WHERE r.status = 'PAID'
            ON CONFLICT DO NOTHING
        """,
        ),
    )

    # 2. Backfill assistant hiring costs
    conn.execute(
        sa.text(
            """
            INSERT INTO credit_transaction
                (billing_account_id, at, amount, balance_after, category,
                 assistant_id, user_id, organization_id, description, detail)
            SELECT
                COALESCE(o.billing_account_id, u.billing_account_id),
                a.created_at,
                -:creation_cost,
                NULL,
                'hire',
                a.agent_id,
                a.user_id,
                a.organization_id,
                'Assistant creation',
                jsonb_build_object(
                    'event', 'backfill_hire',
                    'assistant_id', a.agent_id
                )
            FROM assistants a
            JOIN "user" u ON u.id = a.user_id
            LEFT JOIN organization o ON o.id = a.organization_id
            WHERE a.is_local = false
              AND a.demo_id IS NULL
              AND COALESCE(o.billing_account_id, u.billing_account_id) IS NOT NULL
            ON CONFLICT DO NOTHING
        """,
        ),
        {"creation_cost": ASSISTANT_CREATION_COST},
    )

    # 3. Backfill contact setup (one-time) fees
    conn.execute(
        sa.text(
            """
            INSERT INTO credit_transaction
                (billing_account_id, at, amount, balance_after, category,
                 assistant_id, user_id, organization_id, description, detail)
            SELECT
                COALESCE(o.billing_account_id, u.billing_account_id),
                ac.created_at,
                -ctc.one_time_cost,
                NULL,
                'resources',
                ac.assistant_id,
                a.user_id,
                a.organization_id,
                'Contact setup (' || ac.contact_type || ')',
                jsonb_build_object(
                    'event', 'backfill_contact_setup',
                    'contact_id', ac.id,
                    'contact_type', ac.contact_type,
                    'provider', ac.provider
                )
            FROM assistant_contacts ac
            JOIN assistants a ON a.agent_id = ac.assistant_id
            JOIN "user" u ON u.id = a.user_id
            LEFT JOIN organization o ON o.id = a.organization_id
            LEFT JOIN contact_type_costs ctc
                ON ctc.contact_type = ac.contact_type
                AND (ctc.provider = ac.provider OR (ctc.provider IS NULL AND ac.provider IS NULL))
                AND (ctc.country_code = ac.country_code OR (ctc.country_code IS NULL AND ac.country_code IS NULL))
            WHERE ctc.one_time_cost IS NOT NULL
              AND ctc.one_time_cost > 0
              AND COALESCE(o.billing_account_id, u.billing_account_id) IS NOT NULL
            ON CONFLICT DO NOTHING
        """,
        ),
    )

    # 4. Backfill contact levies (inferred from last_billed_month)
    conn.execute(
        sa.text(
            """
            INSERT INTO credit_transaction
                (billing_account_id, at, amount, balance_after, category,
                 assistant_id, user_id, organization_id, description, detail)
            SELECT
                COALESCE(o.billing_account_id, u.billing_account_id),
                ac.updated_at,
                -ac.monthly_cost,
                NULL,
                'resources',
                ac.assistant_id,
                a.user_id,
                a.organization_id,
                'Contact levy (' || ac.last_billed_month || ')',
                jsonb_build_object(
                    'event', 'backfill_levy',
                    'contact_type', ac.contact_type,
                    'billing_month', ac.last_billed_month
                )
            FROM assistant_contacts ac
            JOIN assistants a ON a.agent_id = ac.assistant_id
            JOIN "user" u ON u.id = a.user_id
            LEFT JOIN organization o ON o.id = a.organization_id
            WHERE ac.last_billed_month IS NOT NULL
              AND ac.monthly_cost IS NOT NULL
              AND ac.monthly_cost > 0
              AND COALESCE(o.billing_account_id, u.billing_account_id) IS NOT NULL
            ON CONFLICT DO NOTHING
        """,
        ),
    )

    # 5. Backfill from LLM cost events in the Assistants project.
    # DISTINCT ON (le.id) avoids duplicates from the log_event_context
    # many-to-many join. LEFT JOIN on "user" includes org-owned projects
    # where project.user_id IS NULL.
    conn.execute(
        sa.text(
            """
            INSERT INTO credit_transaction
                (billing_account_id, at, amount, balance_after, category,
                 assistant_id, user_id, organization_id, description, detail)
            SELECT DISTINCT ON (le.id)
                COALESCE(o.billing_account_id, u.billing_account_id),
                le.created_at,
                -(le.data->>'billed_cost')::numeric,
                NULL,
                'llm',
                (le.data->>'_assistant_id')::integer,
                le.data->>'_user_id',
                p.organization_id,
                'Assistant work',
                jsonb_build_object(
                    'event', 'backfill_llm',
                    'log_event_id', le.id,
                    'provider_cost', (le.data->>'provider_cost')::numeric,
                    'model', le.data->'request'->>'model'
                )
            FROM log_event le
            JOIN log_event_context lec ON le.id = lec.log_event_id
            JOIN context c ON lec.context_id = c.id
            JOIN project p ON c.project_id = p.id
            LEFT JOIN "user" u ON p.user_id = u.id
            LEFT JOIN organization o ON p.organization_id = o.id
            WHERE p.name = 'Assistants'
              AND c.name LIKE '%/Events/LLM'
              AND le.data->>'billed_cost' IS NOT NULL
              AND (le.data->>'billed_cost')::numeric > 0
              AND COALESCE(o.billing_account_id, u.billing_account_id) IS NOT NULL
            ORDER BY le.id
        """,
        ),
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DELETE FROM credit_transaction
            WHERE detail->>'event' IN (
                'backfill_recharge',
                'backfill_hire',
                'backfill_contact_setup',
                'backfill_levy',
                'backfill_llm',
                'backfill_org_llm'
            )
        """,
        ),
    )
