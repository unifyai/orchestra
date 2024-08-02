-- Users
INSERT INTO users VALUES (:user_id, 10, null, False, -1, 0);
INSERT INTO users VALUES ('stripe_autorecharge', 10, null, False, -1, 0);
INSERT INTO users VALUES ('recharge_simple', 1, null, False, -1, 0);
INSERT INTO users VALUES ('recharge_limited', 9.99, null, False, -1, 0);
INSERT INTO users VALUES ('recharge_not_needed_a', 10, null, False, -1, 0);
INSERT INTO users VALUES ('recharge_not_needed_b', 20, null, False, -1, 0);

-- Recharge
INSERT INTO recharge_type VALUES ('free');

-- Provider
INSERT INTO provider VALUES (1, 'openai', '', '');
INSERT INTO provider VALUES (3, 'deepinfra', '', '');
INSERT INTO provider VALUES (4, 'fireworks-ai', '', '');
INSERT INTO provider VALUES (5, 'lepton-ai', '', '');
INSERT INTO provider VALUES (6, 'replicate', '', '');
INSERT INTO provider VALUES (7, 'together-ai', '', '');
INSERT INTO provider VALUES (8, 'mistral-ai', '', '');
INSERT INTO provider VALUES (9, 'octoai', '', '');
INSERT INTO provider VALUES (10, 'perplexity-ai', '', '');
INSERT INTO provider VALUES (11, 'aws-bedrock', '', '');
INSERT INTO provider VALUES (12, 'anthropic', '', '');
INSERT INTO provider VALUES (35, 'groq', '', '');
INSERT INTO provider VALUES (36, 'vertex-ai', '', '');

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
INSERT INTO model VALUES (11, 'mistral-7b-instruct-v0.3', NOW(), 'dummy_task', true);

INSERT INTO endpoint VALUES (1, 9, 2, NOW());
INSERT INTO endpoint VALUES (3, 9, 4, NOW());
INSERT INTO endpoint VALUES (4, 9, 5, NOW());
INSERT INTO endpoint VALUES (5, 9, 6, NOW());
INSERT INTO endpoint VALUES (6, 9, 7, NOW());
INSERT INTO endpoint VALUES (7, 11, 8, NOW());
INSERT INTO endpoint VALUES (8, 11, 9, NOW());
INSERT INTO endpoint VALUES (9, 9, 10, NOW());
INSERT INTO endpoint VALUES (10, 6, 11, NOW());
INSERT INTO endpoint VALUES (11, 9, 11, NOW());
INSERT INTO endpoint VALUES (15, 7, 1, NOW());
INSERT INTO endpoint VALUES (16, 8, 12, NOW());
INSERT INTO endpoint VALUES (35, 10, 36, NOW());
INSERT INTO endpoint VALUES (34, 9, 35, NOW());

-- Runtime Dynamic routing
INSERT INTO model VALUES (4, 'pbr-model', NOW(), 'dummy_task', true);
INSERT INTO model VALUES (5, 'pbr-model-empty-lut', NOW(), 'dummy_task', true);

INSERT INTO endpoint VALUES (12, 4, 13, NOW());
INSERT INTO endpoint VALUES (13, 4, 14, NOW());
INSERT INTO endpoint VALUES (14, 4, 15, NOW());
INSERT INTO endpoint VALUES (17, 4, 18, NOW());

INSERT INTO endpoint VALUES (18, 4, 19, NOW());
INSERT INTO endpoint VALUES (19, 4, 20, NOW());
INSERT INTO endpoint VALUES (20, 4, 21, NOW());
INSERT INTO endpoint VALUES (21, 4, 22, NOW());

INSERT INTO endpoint VALUES (22, 4, 23, NOW());
INSERT INTO endpoint VALUES (23, 4, 24, NOW());
INSERT INTO endpoint VALUES (24, 4, 25, NOW());
INSERT INTO endpoint VALUES (25, 4, 26, NOW());

INSERT INTO endpoint VALUES (26, 4, 27, NOW());
INSERT INTO endpoint VALUES (27, 4, 28, NOW());
INSERT INTO endpoint VALUES (28, 4, 29, NOW());
INSERT INTO endpoint VALUES (29, 4, 30, NOW());

INSERT INTO endpoint VALUES (30, 4, 31, NOW());
INSERT INTO endpoint VALUES (31, 4, 32, NOW());
INSERT INTO endpoint VALUES (32, 4, 33, NOW());
INSERT INTO endpoint VALUES (33, 4, 34, NOW());


-- Benchmark run
INSERT INTO benchmark_regime VALUES('concurrent-1');
INSERT INTO benchmark_region VALUES('Belgium');
INSERT INTO benchmark_seq_len VALUES('short');

INSERT INTO benchmark_run VALUES(3, 12, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(4, 13, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(5, 14, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(8, 17, 'concurrent-1', 'Belgium', 'short', now());

INSERT INTO benchmark_run VALUES(9, 18, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(10, 19, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(11, 20, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(12, 21, 'concurrent-1', 'Belgium', 'short', now());

INSERT INTO benchmark_run VALUES(13, 22, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(14, 23, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(15, 24, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(16, 25, 'concurrent-1', 'Belgium', 'short', now());

INSERT INTO benchmark_run VALUES(17, 26, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(18, 27, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(19, 28, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(20, 29, 'concurrent-1', 'Belgium', 'short', now());

INSERT INTO benchmark_run VALUES(21, 30, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(22, 31, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(23, 32, 'concurrent-1', 'Belgium', 'short', now());
INSERT INTO benchmark_run VALUES(24, 33, 'concurrent-1', 'Belgium', 'short', now());


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
INSERT INTO datapoint VALUES (29, 5, 'itl', 1, NULL, NOW());
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
---- lowest-itl-provider<0.1ic
INSERT INTO datapoint VALUES (56, 11, 'input_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (57, 11, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (58, 11, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (59, 11, 'itl', 9, NULL, NOW());
---- lowest-ttft-provider<0.1ic
INSERT INTO datapoint VALUES (60, 12, 'input_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (61, 12, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (62, 12, 'ttft', 200, NULL, NOW());
INSERT INTO datapoint VALUES (63, 12, 'itl', 10, NULL, NOW());


---- lowest-input-cost-per-token-provider<10ic
INSERT INTO datapoint VALUES (64, 13, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (65, 13, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (66, 13, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (67, 13, 'itl', 10, NULL, NOW());
---- lowest-output-cost-per-token-provider<10ic
INSERT INTO datapoint VALUES (68, 14, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (69, 14, 'output_cost_per_token', 0.01, NULL, NOW());
INSERT INTO datapoint VALUES (70, 14, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (71, 14, 'itl', 10, NULL, NOW());
---- lowest-itl-provider<10ic
INSERT INTO datapoint VALUES (72, 15, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (73, 15, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (74, 15, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (75, 15, 'itl', 5, NULL, NOW());
---- lowest-ttft-provider<10ic
INSERT INTO datapoint VALUES (76, 16, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (77, 16, 'output_cost_per_token', 35, NULL, NOW());
INSERT INTO datapoint VALUES (78, 16, 'ttft', 100, NULL, NOW());
INSERT INTO datapoint VALUES (79, 16, 'itl', 10, NULL, NOW());


---- lowest-input-cost-per-token-provider<0.1oc
INSERT INTO datapoint VALUES (80, 17, 'input_cost_per_token', 0.02, NULL, NOW());
INSERT INTO datapoint VALUES (81, 17, 'output_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (82, 17, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (83, 17, 'itl', 10, NULL, NOW());
---- lowest-output-cost-per-token-provider<0.1oc
INSERT INTO datapoint VALUES (84, 18, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (85, 18, 'output_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (86, 18, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (87, 18, 'itl', 10, NULL, NOW());
---- lowest-itl-provider<0.1oc
INSERT INTO datapoint VALUES (88, 19, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (89, 19, 'output_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (90, 19, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (91, 19, 'itl', 9, NULL, NOW());
---- lowest-ttft-provider<0.1oc
INSERT INTO datapoint VALUES (92, 20, 'input_cost_per_token', 20, NULL, NOW());
INSERT INTO datapoint VALUES (93, 20, 'output_cost_per_token', 0.09, NULL, NOW());
INSERT INTO datapoint VALUES (94, 20, 'ttft', 200, NULL, NOW());
INSERT INTO datapoint VALUES (95, 20, 'itl', 20, NULL, NOW());


---- lowest-input-cost-per-token-provider<10oc
INSERT INTO datapoint VALUES (96, 21, 'input_cost_per_token', 0.01, NULL, NOW());
INSERT INTO datapoint VALUES (97, 21, 'output_cost_per_token', 5, NULL, NOW());
INSERT INTO datapoint VALUES (98, 21, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (99, 21, 'itl', 10, NULL, NOW());
---- lowest-output-cost-per-token-provider<10oc
INSERT INTO datapoint VALUES (100, 22, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (101, 22, 'output_cost_per_token', 5, NULL, NOW());
INSERT INTO datapoint VALUES (102, 22, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (103, 22, 'itl', 10, NULL, NOW());
---- lowest-itl-provider<10oc
INSERT INTO datapoint VALUES (104, 23, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (105, 23, 'output_cost_per_token', 5, NULL, NOW());
INSERT INTO datapoint VALUES (106, 23, 'ttft', 450, NULL, NOW());
INSERT INTO datapoint VALUES (107, 23, 'itl', 5, NULL, NOW());
---- lowest-ttft-provider<10oc
INSERT INTO datapoint VALUES (108, 24, 'input_cost_per_token', 2, NULL, NOW());
INSERT INTO datapoint VALUES (109, 24, 'output_cost_per_token', 5, NULL, NOW());
INSERT INTO datapoint VALUES (110, 24, 'ttft', 100, NULL, NOW());
INSERT INTO datapoint VALUES (111, 24, 'itl', 10, NULL, NOW());
