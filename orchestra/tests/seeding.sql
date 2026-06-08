-- Managed billing v2: default plan_group (id=1).
-- Mirrors the seed in migration 2026-05-04-16-00_metered_and_billing_plan.py.
-- Must be inserted before any billing_account rows because the new
-- ``billing_account.plan_group_id`` column is NOT NULL with default=1
-- and there is a RESTRICT FK to plan_group.id.
INSERT INTO plan_group (
    id, name, display_name, description,
    is_active, created_at
) VALUES (
    1, 'default', 'Default',
    'Platform-default plan group, auto-assigned to every account.',
    true, now()
)
ON CONFLICT (id) DO NOTHING;
SELECT setval(
    pg_get_serial_sequence('plan_group', 'id'),
    GREATEST(1, (SELECT COALESCE(MAX(id), 1) FROM plan_group))
);

-- Billing accounts (shared billing for users)
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (1, 10000, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (2, 10, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (3, 1, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (4, 9.99, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (5, 10, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (6, 20, null, 'ACTIVE', False);
INSERT INTO billing_account (id, credits, stripe_customer_id, account_status, billing_setup_complete)
VALUES (7, 0, null, 'ACTIVE', False);

-- Reset the sequence so new billing accounts get IDs after our seeded ones
SELECT setval('billing_account_id_seq', 7);

-- Users (consolidated user table) - now linked to billing_account
INSERT INTO "user" (id, email, billing_account_id)
VALUES (:user_id, 'test@debug.com', 1);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('stripe_autorecharge', 'stripe@test.com', 2);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('user1', 'user1@test.com', 3);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('user2', 'user2@test.com', 4);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('user3', 'user3@test.com', 5);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('user4', 'user4@test.com', 6);
INSERT INTO "user" (id, email, billing_account_id)
VALUES ('seconday_user', '2nd@user.com', 7);

INSERT INTO api_key("user_id", "key") VALUES (:user_id, :api_key);
INSERT INTO api_key("user_id", "key") VALUES ('seconday_user', '2nd_api_key');

-- Recharge
INSERT INTO recharge_type VALUES ('free');

-- Managed billing v2: implicit-default PAYG template.
-- Mirrors the seed in migration 2026-05-04-16-00_managed_billing_v2_init.py
-- so test DBs (which build the schema via meta.create_all rather than
-- alembic) have the same baseline. Accounts whose plan_assignment_id is NULL
-- semantically resolve to this row.
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
    'Platform-default pay-as-you-go plan. Credit-based wallet with auto-recharge support.',
    'CREDITS',
    NULL, 'USD', NULL, NULL,
    1.0, 1.0,
    'AUTO_CARD',
    'PRORATE', NULL,
    NULL, NULL,
    false, true, now()
);

-- Self-serve subscription tiers (CREDITS / STRIPE_SUBSCRIPTION). Mirrors the
-- seed in migration 2026-06-04-00-00_self_serve_subscription_tiers.py. At
-- 1 credit = $1 the monthly price == the monthly credit grant == the Stripe
-- subscription quantity; the marketing "credit volume" is display-only and
-- lives in the description. ids 2..22 map to ladder positions 1..21.
INSERT INTO billing_plan_template (
    id, name, display_name, description,
    billing_mode,
    commit_amount, currency, commit_period, commit_schedule,
    base_pricing_factor, overage_pricing_factor,
    collection_method,
    proration_policy, credits_rollover_policy,
    fx_policy, fx_locked_rate,
    is_custom, is_active, created_at
) VALUES
    (2,  'tier_50',    '$50 / mo',     '50 credits / month self-serve plan ($50/mo at 1 credit = $1).',         'CREDITS', 50,    'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (3,  'tier_75',    '$75 / mo',     '75 credits / month self-serve plan ($75/mo at 1 credit = $1).',         'CREDITS', 75,    'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (4,  'tier_100',   '$100 / mo',    '100 credits / month self-serve plan ($100/mo at 1 credit = $1).',       'CREDITS', 100,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (5,  'tier_200',   '$200 / mo',    '200 credits / month self-serve plan ($200/mo at 1 credit = $1).',       'CREDITS', 200,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (6,  'tier_300',   '$300 / mo',    '300 credits / month self-serve plan ($300/mo at 1 credit = $1).',       'CREDITS', 300,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (7,  'tier_400',   '$400 / mo',    '400 credits / month self-serve plan ($400/mo at 1 credit = $1).',       'CREDITS', 400,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (8,  'tier_500',   '$500 / mo',    '500 credits / month self-serve plan ($500/mo at 1 credit = $1).',       'CREDITS', 500,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (9,  'tier_750',   '$750 / mo',    '750 credits / month self-serve plan ($750/mo at 1 credit = $1).',       'CREDITS', 750,   'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (10, 'tier_1000',  '$1,000 / mo',  '1,000 credits / month self-serve plan ($1,000/mo at 1 credit = $1).',   'CREDITS', 1000,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (11, 'tier_1500',  '$1,500 / mo',  '1,500 credits / month self-serve plan ($1,500/mo at 1 credit = $1).',   'CREDITS', 1500,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (12, 'tier_2000',  '$2,000 / mo',  '2,000 credits / month self-serve plan ($2,000/mo at 1 credit = $1).',   'CREDITS', 2000,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (13, 'tier_3000',  '$3,000 / mo',  '3,000 credits / month self-serve plan ($3,000/mo at 1 credit = $1).',   'CREDITS', 3000,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (14, 'tier_4000',  '$4,000 / mo',  '4,000 credits / month self-serve plan ($4,000/mo at 1 credit = $1).',   'CREDITS', 4000,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (15, 'tier_5000',  '$5,000 / mo',  '5,000 credits / month self-serve plan ($5,000/mo at 1 credit = $1).',   'CREDITS', 5000,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (16, 'tier_7500',  '$7,500 / mo',  '7,500 credits / month self-serve plan ($7,500/mo at 1 credit = $1).',   'CREDITS', 7500,  'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (17, 'tier_10000', '$10,000 / mo', '10,000 credits / month self-serve plan ($10,000/mo at 1 credit = $1).', 'CREDITS', 10000, 'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (18, 'tier_12500', '$12,500 / mo', '12,500 credits / month self-serve plan ($12,500/mo at 1 credit = $1).', 'CREDITS', 12500,'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (19, 'tier_15000', '$15,000 / mo', '15,000 credits / month self-serve plan ($15,000/mo at 1 credit = $1).', 'CREDITS', 15000,'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (20, 'tier_20000', '$20,000 / mo', '20,000 credits / month self-serve plan ($20,000/mo at 1 credit = $1).', 'CREDITS', 20000,'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (21, 'tier_25000', '$25,000 / mo', '25,000 credits / month self-serve plan ($25,000/mo at 1 credit = $1).', 'CREDITS', 25000,'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (22, 'tier_30000', '$30,000 / mo', '30,000 credits / month self-serve plan ($30,000/mo at 1 credit = $1).', 'CREDITS', 30000,'USD', 'MONTHLY', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now());

-- Annual self-serve tiers (CREDITS / STRIPE_SUBSCRIPTION, commit_period
-- ANNUAL). Mirrors migration 2026-06-05-00-00_annual_subscription_tiers.py.
-- commit_amount stays the per-month rung (== the monthly sibling == the
-- Stripe quantity); the annual list price is 12× the rung and the credit
-- grant is the year up front (12×). ids 23..43; ladder positions 101..121.
INSERT INTO billing_plan_template (
    id, name, display_name, description,
    billing_mode,
    commit_amount, currency, commit_period, commit_schedule,
    base_pricing_factor, overage_pricing_factor,
    collection_method,
    proration_policy, credits_rollover_policy,
    fx_policy, fx_locked_rate,
    is_custom, is_active, created_at
) VALUES
    (23, 'tier_50_annual',    '$600 / yr',     '600 credits / year self-serve plan ($600/yr at 1 credit = $1, billed annually).',         'CREDITS', 50,    'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (24, 'tier_75_annual',    '$900 / yr',     '900 credits / year self-serve plan ($900/yr at 1 credit = $1, billed annually).',         'CREDITS', 75,    'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (25, 'tier_100_annual',   '$1,200 / yr',   '1,200 credits / year self-serve plan ($1,200/yr at 1 credit = $1, billed annually).',     'CREDITS', 100,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (26, 'tier_200_annual',   '$2,400 / yr',   '2,400 credits / year self-serve plan ($2,400/yr at 1 credit = $1, billed annually).',     'CREDITS', 200,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (27, 'tier_300_annual',   '$3,600 / yr',   '3,600 credits / year self-serve plan ($3,600/yr at 1 credit = $1, billed annually).',     'CREDITS', 300,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (28, 'tier_400_annual',   '$4,800 / yr',   '4,800 credits / year self-serve plan ($4,800/yr at 1 credit = $1, billed annually).',     'CREDITS', 400,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (29, 'tier_500_annual',   '$6,000 / yr',   '6,000 credits / year self-serve plan ($6,000/yr at 1 credit = $1, billed annually).',     'CREDITS', 500,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (30, 'tier_750_annual',   '$9,000 / yr',   '9,000 credits / year self-serve plan ($9,000/yr at 1 credit = $1, billed annually).',     'CREDITS', 750,   'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (31, 'tier_1000_annual',  '$12,000 / yr',  '12,000 credits / year self-serve plan ($12,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 1000,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (32, 'tier_1500_annual',  '$18,000 / yr',  '18,000 credits / year self-serve plan ($18,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 1500,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (33, 'tier_2000_annual',  '$24,000 / yr',  '24,000 credits / year self-serve plan ($24,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 2000,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (34, 'tier_3000_annual',  '$36,000 / yr',  '36,000 credits / year self-serve plan ($36,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 3000,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (35, 'tier_4000_annual',  '$48,000 / yr',  '48,000 credits / year self-serve plan ($48,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 4000,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (36, 'tier_5000_annual',  '$60,000 / yr',  '60,000 credits / year self-serve plan ($60,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 5000,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (37, 'tier_7500_annual',  '$90,000 / yr',  '90,000 credits / year self-serve plan ($90,000/yr at 1 credit = $1, billed annually).',   'CREDITS', 7500,  'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (38, 'tier_10000_annual', '$120,000 / yr', '120,000 credits / year self-serve plan ($120,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 10000, 'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (39, 'tier_12500_annual', '$150,000 / yr', '150,000 credits / year self-serve plan ($150,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 12500,'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (40, 'tier_15000_annual', '$180,000 / yr', '180,000 credits / year self-serve plan ($180,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 15000,'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (41, 'tier_20000_annual', '$240,000 / yr', '240,000 credits / year self-serve plan ($240,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 20000,'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (42, 'tier_25000_annual', '$300,000 / yr', '300,000 credits / year self-serve plan ($300,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 25000,'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now()),
    (43, 'tier_30000_annual', '$360,000 / yr', '360,000 credits / year self-serve plan ($360,000/yr at 1 credit = $1, billed annually).', 'CREDITS', 30000,'USD', 'ANNUAL', 'AMORTISED', 1.0, 1.0, 'STRIPE_SUBSCRIPTION', 'PRORATE', 'FORFEIT_AT_PERIOD_END', NULL, NULL, false, true, now());

SELECT setval(
    pg_get_serial_sequence('billing_plan_template', 'id'),
    GREATEST(1, (SELECT COALESCE(MAX(id), 1) FROM billing_plan_template))
);

-- Managed billing v2: link the default template into the default group so
-- the platform-default ladder has at least one member (mirrors the migration).
INSERT INTO plan_group_member (group_id, template_id, position, added_at)
VALUES (1, 1, 0, now())
ON CONFLICT (group_id, template_id) DO NOTHING;

-- Self-serve tiers join the default group as ascending ladder rungs
-- (positions 1..21; the default template stays position 0 / free state).
INSERT INTO plan_group_member (group_id, template_id, position, added_at)
VALUES
    (1, 2, 1, now()), (1, 3, 2, now()), (1, 4, 3, now()), (1, 5, 4, now()),
    (1, 6, 5, now()), (1, 7, 6, now()), (1, 8, 7, now()), (1, 9, 8, now()),
    (1, 10, 9, now()), (1, 11, 10, now()), (1, 12, 11, now()), (1, 13, 12, now()),
    (1, 14, 13, now()), (1, 15, 14, now()), (1, 16, 15, now()), (1, 17, 16, now()),
    (1, 18, 17, now()), (1, 19, 18, now()), (1, 20, 19, now()), (1, 21, 20, now()),
    (1, 22, 21, now())
ON CONFLICT (group_id, template_id) DO NOTHING;

-- Annual self-serve tiers join the same default group on a distinct
-- position band (101..121) so the interval-aware ladder orders them
-- independently of the monthly rungs (1..21) without collisions.
INSERT INTO plan_group_member (group_id, template_id, position, added_at)
VALUES
    (1, 23, 101, now()), (1, 24, 102, now()), (1, 25, 103, now()), (1, 26, 104, now()),
    (1, 27, 105, now()), (1, 28, 106, now()), (1, 29, 107, now()), (1, 30, 108, now()),
    (1, 31, 109, now()), (1, 32, 110, now()), (1, 33, 111, now()), (1, 34, 112, now()),
    (1, 35, 113, now()), (1, 36, 114, now()), (1, 37, 115, now()), (1, 38, 116, now()),
    (1, 39, 117, now()), (1, 40, 118, now()), (1, 41, 119, now()), (1, 42, 120, now()),
    (1, 43, 121, now())
ON CONFLICT (group_id, template_id) DO NOTHING;

-- REMOVED: Legacy tables that have been deleted
-- The following sections have been removed because the tables no longer exist:
-- - provider (deleted in migration 2026-01-15-14-00)
-- - modality (deleted in migration 2026-01-15-14-00)
-- - task (deleted in migration 2026-01-15-14-00)
-- - model (deleted in migration 2026-01-15-14-00)
-- - endpoint (deleted in migration 2026-01-15-14-00)
-- - benchmark_regime, benchmark_region, benchmark_seq_len, benchmark_run (deleted in migration 2026-01-15-14-00)
-- - metric (deleted in migration 2026-01-15-14-00)
-- - datapoint (deleted in migration 2026-01-15-14-00)

-- RBAC: Permissions (project, org, billing, and assistant)
INSERT INTO permission (name, description, resource_type, action) VALUES
('project:read', 'View project details', 'project', 'read'),
('project:write', 'Edit project', 'project', 'write'),
('project:delete', 'Delete project', 'project', 'delete'),
('org:read', 'View organization details', 'organization', 'read'),
('org:write', 'Edit organization settings, billing, and members', 'organization', 'write'),
('org:delete', 'Delete organization', 'organization', 'delete'),
('billing:read', 'View billing information, credits, and invoices', 'billing', 'read'),
('billing:write', 'Update billing settings, autorecharge, and business profile', 'billing', 'write'),
('assistant:read', 'View assistant details', 'assistant', 'read'),
('assistant:write', 'Create and edit assistants', 'assistant', 'write'),
('assistant:delete', 'Delete assistants', 'assistant', 'delete');

-- RBAC: System Roles
INSERT INTO role (name, description, organization_id, is_system_role) VALUES
('Owner', 'Full access to projects and organization', NULL, true),
('Admin', 'Full access except deleting organization', NULL, true),
('Member', 'Read and write projects, view organization details', NULL, true),
('Viewer', 'Read-only access to projects and organization', NULL, true);

-- RBAC: Owner role gets all permissions (including billing)
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true), id FROM permission;

-- RBAC: Admin role gets all except org:delete (including billing:read and billing:write)
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Admin' AND is_system_role = true), id
FROM permission WHERE name != 'org:delete';

-- RBAC: Member role gets project read/write + org read + billing read + assistant read/write
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Member' AND is_system_role = true), id
FROM permission
WHERE (resource_type = 'project' AND action IN ('read', 'write'))
   OR (resource_type = 'organization' AND action = 'read')
   OR (resource_type = 'assistant' AND action IN ('read', 'write'))
   OR name = 'billing:read';

-- RBAC: Viewer role gets read only (including billing:read)
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Viewer' AND is_system_role = true), id
FROM permission WHERE action = 'read';
