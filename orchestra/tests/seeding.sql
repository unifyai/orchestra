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
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (1, 10000, null, False, 0, 25, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (2, 10, null, False, -1, 0, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (3, 1, null, False, 0, 25, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (4, 9.99, null, False, 0, 25, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (5, 10, null, False, 0, 25, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (6, 20, null, False, 0, 25, 'ACTIVE', False, 'developer');
INSERT INTO billing_account (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, billing_setup_complete, tier)
VALUES (7, 0, null, False, 0, 25, 'ACTIVE', False, 'developer');

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
SELECT setval(
    pg_get_serial_sequence('billing_plan_template', 'id'),
    GREATEST(1, (SELECT COALESCE(MAX(id), 1) FROM billing_plan_template))
);

-- Managed billing v2: link the default template into the default group so
-- the platform-default ladder has at least one member (mirrors the migration).
INSERT INTO plan_group_member (group_id, template_id, position, added_at)
VALUES (1, 1, 0, now())
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
