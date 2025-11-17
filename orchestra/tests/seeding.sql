-- Users
INSERT INTO users VALUES (:user_id, 10000, null, False, -1, 0);
INSERT INTO users VALUES ('stripe_autorecharge', 10, null, False, -1, 0);
INSERT INTO users VALUES ('user1', 1, null, False, -1, 0);
INSERT INTO users VALUES ('user2', 9.99, null, False, -1, 0);
INSERT INTO users VALUES ('user3', 10, null, False, -1, 0);
INSERT INTO users VALUES ('user4', 20, null, False, -1, 0);

INSERT INTO auth_user("id", "email") VALUES (:user_id, 'test@debug.com');
INSERT INTO auth_user("id", "email") VALUES ('seconday_user', '2nd@user.com');

INSERT INTO api_key("user_id", "key") VALUES (:user_id, :api_key);
INSERT INTO api_key("user_id", "key") VALUES ('seconday_user', '2nd_api_key');

-- Recharge
INSERT INTO recharge_type VALUES ('free');

-- Provider
INSERT INTO provider VALUES (1, 'openai', '', '');
INSERT INTO provider VALUES (3, 'deepinfra', '', '');
INSERT INTO provider VALUES (4, 'fireworks-ai', '', '');
INSERT INTO provider VALUES (6, 'replicate', '', '');
INSERT INTO provider VALUES (7, 'together-ai', '', '');
INSERT INTO provider VALUES (8, 'mistral-ai', '', '');
INSERT INTO provider VALUES (11, 'aws-bedrock', '', '');
INSERT INTO provider VALUES (12, 'anthropic', '', '');
INSERT INTO provider VALUES (35, 'groq', '', '');
INSERT INTO provider VALUES (36, 'vertex-ai', '', '');
INSERT INTO provider VALUES (37, 'deepseek', '', '');
INSERT INTO provider VALUES (38, 'xai', '', '');

INSERT INTO provider VALUES (13, 'lowest-input-cost-per-token-provider', '', '');
INSERT INTO provider VALUES (14, 'lowest-output-cost-per-token-provider', '', '');
INSERT INTO provider VALUES (15, 'lowest-itl-provider', '', '');
INSERT INTO provider VALUES (18, 'lowest-ttft-provider', '', '');
INSERT INTO provider VALUES (19, 'lowest-input-cost-per-token<0.1ic-provider', '', '');
INSERT INTO provider VALUES (20, 'lowest-output-cost-per-token<0.1ic-provider', '', '');
INSERT INTO provider VALUES (21, 'lowest-itl<0.1ic-provider', '', '');
INSERT INTO provider VALUES (22, 'lowest-ttft<0.1ic-provider', '', '');
INSERT INTO provider VALUES (23, 'lowest-input-cost-per-token<10ic-provider', '', '');
INSERT INTO provider VALUES (24, 'lowest-output-cost-per-token<10ic-provider', '', '');
INSERT INTO provider VALUES (25, 'lowest-itl<10ic-provider', '', '');
INSERT INTO provider VALUES (26, 'lowest-ttft<10ic-provider', '', '');
INSERT INTO provider VALUES (27, 'lowest-input-cost-per-token<0.1oc-provider', '', '');
INSERT INTO provider VALUES (28, 'lowest-output-cost-per-token<0.1oc-provider<0.1oc', '', '');
INSERT INTO provider VALUES (29, 'lowest-itl<0.1oc-provider', '', '');
INSERT INTO provider VALUES (30, 'lowest-ttft<0.1oc-provider', '', '');
INSERT INTO provider VALUES (31, 'lowest-input-cost-per-token<10oc-provider', '', '');
INSERT INTO provider VALUES (32, 'lowest-output-cost-per-token<10oc-provider', '', '');
INSERT INTO provider VALUES (33, 'lowest-itl<10oc-provider', '', '');
INSERT INTO provider VALUES (34, 'lowest-ttft<10oc-provider', '', '');

-- Model general
INSERT INTO modality VALUES ('dummy_modality');
INSERT INTO task VALUES ('dummy_task', 'dummy_modality');

-- LLMs
INSERT INTO model VALUES (1, 'llama-2-7b-chat', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (3, 'mistral-7b-instruct-v0.2', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (6, 'llama-2-13b-chat', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (7, 'gpt-3.5-turbo', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (8, 'claude-3-haiku', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (9, 'llama-3-8b-chat', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (10, 'gemini-1.5-flash', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (11, 'mistral-small', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (12, 'claude-4.5-sonnet', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (13, 'llama-3.1-8b-chat', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (14, 'deepseek-v3', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (15, 'grok-4', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (16, 'llama-3.1-405b-chat', NOW(), 'dummy_task', true);

INSERT INTO endpoint VALUES (1, 9, 3, NOW(), true);
INSERT INTO endpoint VALUES (3, 13, 4, NOW(), true);
INSERT INTO endpoint VALUES (5, 16, 6, NOW(), true);
INSERT INTO endpoint VALUES (6, 16, 7, NOW(), true);
INSERT INTO endpoint VALUES (7, 11, 8, NOW(), true);
INSERT INTO endpoint VALUES (10, 6, 11, NOW(), true);
INSERT INTO endpoint VALUES (11, 9, 11, NOW(), true);
INSERT INTO endpoint VALUES (15, 7, 1, NOW(), true);
INSERT INTO endpoint VALUES (16, 8, 12, NOW(), true);
INSERT INTO endpoint VALUES (34, 13, 35, NOW(), true);
INSERT INTO endpoint VALUES (36, 12, 12, NOW(), true);
INSERT INTO endpoint VALUES (37, 10, 36, NOW(), true);
INSERT INTO endpoint VALUES (38, 13, 3, NOW(), true);
INSERT INTO endpoint VALUES (39, 14, 37, NOW(), true);
INSERT INTO endpoint VALUES (40, 15, 38, NOW(), true);

-- Runtime Dynamic routing
INSERT INTO model VALUES (4, 'pbr-model', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (5, 'pbr-model-empty-lut', NOW(), 'dummy_task', true);

-- Benchmark run
INSERT INTO benchmark_regime VALUES('concurrent-1');
INSERT INTO benchmark_region VALUES('Belgium');
INSERT INTO benchmark_seq_len VALUES('short');

INSERT INTO benchmark_run VALUES(3, 7, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(4, 10, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(5, 11, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(8, 15, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(9, 16, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(10, 38, 'concurrent-1', 'Belgium', 'short', now());

-- Metrics
INSERT INTO metric VALUES ('input_cost_per_token', '$/1M tks', 'Input Cost', 'Input cost per token', 1, 'f');
INSERT INTO metric VALUES ('output_cost_per_token', '$/1M tks', 'Output Cost', 'Output cost per token', 1, 'f');
INSERT INTO metric VALUES ('ttft', 'TFTT', 'Time to First Token', 'ms', 1, 't');
INSERT INTO metric VALUES ('output_tks_per_sec', 'tks/sec', 'Output Tks / Sec', 'Output Tokens per Second', 5, 't');
INSERT INTO metric VALUES ('itl', 'ms', 'ITL', 'Inter Token Latency', 10, 't');
INSERT INTO metric VALUES ('e2e_latency', 'ms', 'E2E Latency', 'End-to-End Latency', 15, 't');
INSERT INTO metric VALUES ('cold_start', 'ms', 'Cold Start', 'Cold Start', 20, 't');

-- Datapoint
---- lowest-input-cost-per-token-provider
INSERT INTO datapoint VALUES (13, 3, 'input_cost_per_token', 0.01, NULL, NOW());
INSERT INTO datapoint VALUES (14, 3, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (16, 3, 'ttft', 4500, NULL, NOW());
INSERT INTO datapoint VALUES (17, 3, 'itl', 1000, NULL, NOW());
---- lowest-output-cost-per-token-provider
INSERT INTO datapoint VALUES (19, 4, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (20, 4, 'output_cost_per_token', 0.01, NULL, NOW());
INSERT INTO datapoint VALUES (22, 4, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (23, 4, 'itl', 10, NULL, NOW());
---- lowest-itl-provider
INSERT INTO datapoint VALUES (25, 5, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (26, 5, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (28, 5, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (29, 5, 'itl', 4, NULL, NOW());
---- lowest-ttft-provider
INSERT INTO datapoint VALUES (43, 8, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (44, 8, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (46, 8, 'ttft', 50, NULL, NOW());
INSERT INTO datapoint VALUES (47, 8, 'itl', 10, NULL, NOW());


---- lowest-input-cost-per-token-provider<0.1ic
INSERT INTO datapoint VALUES (48, 9, 'input_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (49, 9, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (50, 9, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (51, 9, 'itl', 10, NULL, NOW());

---- lowest-output-cost-per-token-provider<0.1ic
INSERT INTO datapoint VALUES (52, 10, 'input_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (53, 10, 'output_cost_per_token', 0.02, NULL, NOW());
INSERT INTO datapoint VALUES (54, 10, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (55, 10, 'itl', 10, NULL, NOW());

-- RBAC: Permissions (project and org only)
INSERT INTO permission (name, description, resource_type, action) VALUES
('project:read', 'View project details', 'project', 'read'),
('project:write', 'Edit project', 'project', 'write'),
('project:delete', 'Delete project', 'project', 'delete'),
('org:read', 'View organization details', 'organization', 'read'),
('org:write', 'Edit organization settings, billing, and members', 'organization', 'write'),
('org:delete', 'Delete organization', 'organization', 'delete');

-- RBAC: System Roles
INSERT INTO role (name, description, organization_id, is_system_role) VALUES
('Owner', 'Full access to projects and organization', NULL, true),
('Admin', 'Full access except deleting organization', NULL, true),
('Member', 'Read and write projects, view organization details', NULL, true),
('Viewer', 'Read-only access to projects and organization', NULL, true);

-- RBAC: Owner role gets all permissions
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true), id FROM permission;

-- RBAC: Admin role gets all except org:delete
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Admin' AND is_system_role = true), id
FROM permission WHERE name != 'org:delete';

-- RBAC: Member role gets project read/write + org read
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Member' AND is_system_role = true), id
FROM permission
WHERE (resource_type = 'project' AND action IN ('read', 'write'))
   OR (resource_type = 'organization' AND action = 'read');

-- RBAC: Viewer role gets read only
INSERT INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE name = 'Viewer' AND is_system_role = true), id
FROM permission WHERE action = 'read';
