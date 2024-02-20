-- Users
INSERT INTO users VALUES (:user_id, 10);
INSERT INTO users VALUES ('recharge_simple', 1);
INSERT INTO users VALUES ('recharge_limited', 9.99);
INSERT INTO users VALUES ('recharge_not_needed_a', 10);
INSERT INTO users VALUES ('recharge_not_needed_b', 20);

-- Recharge
INSERT INTO recharge_type VALUES ('free');

-- Provider
INSERT INTO provider VALUES (1, 'openai', '', '');
INSERT INTO provider VALUES (2, 'anyscale', '', '');
INSERT INTO provider VALUES (3, 'deepinfra', '', '');
INSERT INTO provider VALUES (4, 'fireworks-ai', '', '');
INSERT INTO provider VALUES (5, 'lepton-ai', '', '');
INSERT INTO provider VALUES (6, 'replicate', '', '');
INSERT INTO provider VALUES (7, 'together-ai', '', '');
INSERT INTO provider VALUES (8, 'mistral-ai', '', '');
INSERT INTO provider VALUES (9, 'octoai', '', '');
INSERT INTO provider VALUES (10, 'perplexity-ai', '', '');

-- Model general
INSERT INTO license VALUES ('dummy_license', '', '');
INSERT INTO modality VALUES ('dummy_modality');
INSERT INTO task VALUES ('dummy_task', 'dummy_modality');

-- LLMs
INSERT INTO model VALUES (1, 'llama-2-7b-chat', :user_id, NOW(), 'dummy_task', '', 'dummy_license', '', '', '', true, false);
INSERT INTO model VALUES (2, 'mistral-7b-instruct-v0.1', :user_id, NOW(), 'dummy_task', '', 'dummy_license', '', '', '', true, false);
INSERT INTO model VALUES (3, 'mistral-7b-instruct-v0.2', :user_id, NOW(), 'dummy_task', '', 'dummy_license', '', '', '', true, false);
INSERT INTO endpoint VALUES (1, 1, 2, NOW());
INSERT INTO endpoint VALUES (2, 1, 3, NOW());
INSERT INTO endpoint VALUES (3, 1, 4, NOW());
INSERT INTO endpoint VALUES (4, 1, 5, NOW());
INSERT INTO endpoint VALUES (5, 1, 6, NOW());
INSERT INTO endpoint VALUES (6, 1, 7, NOW());
INSERT INTO endpoint VALUES (7, 3, 8, NOW());
INSERT INTO endpoint VALUES (8, 2, 9, NOW());
INSERT INTO endpoint VALUES (9, 3, 10, NOW());
