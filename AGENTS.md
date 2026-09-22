<!--
    GENERATED FILE - DO NOT EDIT DIRECTLY.

    Regenerate with:  python3 .agents/global-rules/build_agents_md.py

    Edit the sources instead:
      .agents/repo.md              this repo's overview and always-on guidance
      .agents/rules/*.md           this repo's own rules
      .agents/shared.txt           which shared rules this repo includes
      .agents/global-rules/rules/  rules shared across all unifyai repos
                                   (submodule: unifyai/global-agent-rules)
-->

# Orchestra: The Backend API & Database Layer


Orchestra is a single repository backing all persistent state on a Postgres database. It owns every table — the data primitives (`project`, `project_version`, `context`, `context_counter`, `context_version`, `log_event`, `log_event_context`, `log_event_version`, `active_derived_log_template`, `log_unique_constraint`, `field_type`, `embedding`, `embedding_queue`) and the tenant-aware tables (`user`, `organization`, `billing_account`, `assistants`, `api_key`, `voices`, `space`, `team`, `interface`, `tile`, etc.) — plus all services (Stripe, Twilio, ElevenLabs, OAuth, MFA, etc.).

## Responsibilities

- User accounts, authentication, and API keys
- Projects, contexts, and logging/metrics (with multi-tenant access control)
- Assistants (profiles, phone numbers, voice settings, desktop URLs)
- Billing, credits, and Stripe webhook handling
- Interfaces, tabs, and tiles for Console UI state
- Object storage and signed URL generation
- Observability infrastructure (Prometheus, Loki, Tempo, Grafana, OTel)

## Architecture

The codebase follows a clean layered structure: `web/api/` contains FastAPI routers, `db/dao/` contains data access objects, `db/models/` contains SQLAlchemy ORM models, and `db/migrations/` contains Alembic migrations. Services in `services/` handle external integrations. All models register against the shared `orchestra.db.meta.meta` MetaData.

## Alembic chain

There is a single Alembic chain under `orchestra/db/migrations/versions/`, applied by `orchestra/db/migrations/env.py` (which loads all models before running). When adding a migration, set `down_revision` to the current chain leaf.

## Position in the System

Orchestra is the source of truth for all persistent data. When Unify's managers call UniSDK (e.g. `unisdk.log()`) or store contacts/tasks/knowledge, those requests hit Orchestra's endpoints. The Console reads from and writes to Orchestra for all UI state. The hosted communication stack in `unify-deploy` authenticates against Orchestra for admin operations.

## Related Repositories

- **unify**: The AI brain that persists its state to Orchestra
- **unisdk**: Python SDK that wraps Orchestra's REST API
- **console**: Web UI that reads/writes Orchestra data
- **unify-deploy**: Hosted communication stack that authenticates with Orchestra for admin operations
- **unillm**: Independent (no direct Orchestra dependency)

## Mirrored Artifacts

- **OAuth Scopes** (`web/api/assistant/scopes.py`): Mirrored in `unify-deploy/common/scopes.py`. Any change MUST be applied in both repos in the same changeset. See `scopes-mirror.md`.

---

# Repository rules

# Local Development Environment

## Package Manager

This project uses **uv** for dependency management, like every other first-party
Python repo. Do not use pip, poetry, or other package managers.

## Python Interpreter

- The virtualenv is the repo-local `.venv`; the interpreter is always
  `.venv/bin/python`.
- Use `uv run` to execute commands within the virtual environment.

## Environment Bootstrap

If the environment is not set up, install dependencies with:

```bash
uv sync
```

This installs the project plus the `dev` group (which includes `lint`).

## Database Setup

Orchestra requires a PostgreSQL database with the `pgvector` extension. Run the pgvector-enabled container:

```bash
docker run --name orchestra-db -p 5432:5432 \
  -e POSTGRES_PASSWORD=orchestra -e POSTGRES_USER=orchestra -e POSTGRES_DB=orchestra \
  pgvector/pgvector:pg15
```

After starting a new container, run migrations:

```bash
uv run alembic upgrade "head"
```

To populate the database with model/provider data:

```bash
uv run python add_latest_endpoint_data.py <user_id> <email_id> <api_key>
```

## Running Tests

### Prerequisites

Tests require the PostgreSQL container running (see Database Setup above). Some tests also require secrets and environment variables.

### Basic Commands

```bash
# Single test file
uv run pytest orchestra/tests/path/to/test.py -vv

# Specific test function
uv run pytest orchestra/tests/path/to/test.py::test_function_name -vv

# Entire directory
uv run pytest orchestra/tests/test_some_module/ -vv

# All tests
uv run pytest -vv .
```

### Common Issues

If you see `extension "vector" is not available`, your Postgres instance lacks pgvector. Use the `pgvector/pgvector:pg15` image or run `CREATE EXTENSION IF NOT EXISTS vector;` in your database.

## Running Orchestra Locally

Start the service:

```bash
uv run python -m orchestra
```

Or with uvicorn for hot reload:

```bash
uv run uvicorn orchestra.web.application:get_app --reload
```

The API will be available at http://127.0.0.1:8000/v0

### Login autostart (macOS, optional)

To have the local server come back on its own after a reboot, install the
per-user LaunchAgent once per machine:

```bash
bash scripts/install_login_autostart.sh
```

RunAtLoad only — no KeepAlive, so `scripts/local.sh stop` still means stopped
and a crashed server stays down for inspection. launchd owns the process, so
the server does not die with the shell or agent session that started it.

## Pre-commit Hooks

Enable the committed hooks once per clone/worktree:

```bash
python3 .agents/global-rules/ensure_git_hooks.py
```

Pre-commit hooks run automatically on `git commit`. If a commit fails due to auto-formatting, simply re-run the commit command - the hooks will have fixed the files.

## Environment Variables

- Environment variables should start with `ORCHESTRA_` prefix.
- VSCode loads `.env` file by default.
- Example variables: `ORCHESTRA_RELOAD`, `ORCHESTRA_PORT`, `ORCHESTRA_ENVIRONMENT`

## Dependencies

- Config file: `pyproject.toml`
- Lock file: `uv.lock`
- Do not edit `uv.lock` manually. Use `uv add`, `uv remove`, or `uv lock --upgrade-package <name>`.

## Observability (Local)

For local observability stack (Prometheus, Loki, Tempo, Grafana):

```bash
docker-compose -f orchestra/observability/docker-compose.observability.yml up -d
```

Access Grafana at http://localhost:3000

# There Is No CodeQL Workflow Here, Deliberately

Do **not** add `.github/workflows/codeql.yml` to this repo. One existed until
2026-08-05 and was deleted because it could not do anything useful.

## Why it could not work

Orchestra is **private** and has `code_security: disabled`:

```bash
gh api repos/unifyai/orchestra --jq '.security_and_analysis.code_security.status'
# disabled
```

Code scanning results can only be uploaded to a private repo when Code Security
is enabled — it is a paid feature. Without it, `github/codeql-action/analyze`
fails on upload.

The workflow worked around that with `upload: false`, added on 2026-06-19 inside
a commit titled *"Fix orchestra staging promotion checks"* that never mentioned
CodeQL. From then until deletion it ran a full `security-extended` Python sweep
on every push and pull request, with a thirty-minute timeout, and **discarded
every finding**. The job reported success throughout, so the repo appeared to be
scanned when nothing was being recorded — including whatever it had to say about
the sixteen workflows here that carry no `permissions:` block.

Neither `CodeQL` nor `Analyze (python)` was ever a required check in the
`Staging->Main` ruleset (`pytest`, `staging-source`, `black`,
`should-run-tests`, `unify-orchestra-staging`), so removing it changed no gate.

## What to do instead

Scanning this repo needs Code Security enabled first — an org-level spend
decision, not a workflow change. Once it is on, add the workflow back **with
uploads on** (`upload: true`, or simply omit the input) and include the
`actions` language alongside `python`; the actions queries are what flag the
missing `permissions:` blocks.

Until then, a workflow here buys a green check and no coverage. Public repos in
the org get this free via GitHub's default setup — `unify`, `unisdk`, `unillm`
and `docs` all run the `extended` suite that way, with no file to maintain. See
`unify/.agents/rules/codeql-runs-from-default-setup.md`.
