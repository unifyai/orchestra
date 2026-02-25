# Orchestra

## System Architecture

Orchestra is the backend API and database layer in a multi-repository system:

```
         User (Console/Phone/SMS/Email)
                      │
    ┌─────────────────┴──────────────────┐
    │           Communication            │
    │    (Webhooks, Voice, SMS, Email)   │
    └────┬───────────────────────────────┘
         │
    ┌────┴────┐    ┌─────────┐    ┌─────────┐
    │  Unity  │    │  Unify  │    │Orchestra│
    │ (Brain) │───▶│  (SDK)  │───▶│  (API)  │
    │         │    │         │    │  (DB)   │
    └────┬────┘    └────┬────┘    └────┬────┘
         │              ▲              ▲
         │              │              │
         │    ┌─────────┴─┐       ┌────┴───────┐
         └───▶│  UniLLM   │       │  Console   │
              │ (LLM API) │       │(Interfaces)│
              └───────────┘       └────────────┘
```

**This repo (Orchestra)** is the source of truth for all persistent data. It provides the REST API at `api.unify.ai/v0` that Unify (Python SDK) and Console (web UI) communicate with.

Related repositories:
- [Unify](https://github.com/unifyai/unify) — Python SDK that wraps Orchestra's API
- [Console](https://github.com/unifyai/console) — Web UI that reads/writes Orchestra data
- [Unity](https://github.com/unifyai/unity) — AI assistant brain (persists state via Unify)

---

This repo includes the code for Orchestra, the core API server and database layer used by Unity, Communication, Unify, and Console.

- Orchestra Production API URL: https://api.unify.ai/v0
- Orchestra Staging API URL: https://orchestra-staging-lz5fmz6i7q-ew.a.run.app/v0

## Security

### Authentication

All API endpoints under `/v0/` require a Bearer token via the `Authorization` header, except:
- `GET /v0/health` — unauthenticated health check
- `POST /v0/webhooks/stripe` — Stripe signature verification (no API key)

Admin endpoints under `/v0/admin/` require the `ORCHESTRA_ADMIN_KEY`. This key comparison uses `secrets.compare_digest()` for timing-attack resistance. Admin endpoints also support OIDC token verification from the `CLOUD_SCHEDULER_SERVICE_ACCOUNT` service account.

### Prometheus Metrics

The `/metrics` endpoint requires Bearer token authentication via the `PROMETHEUS_METRICS_TOKEN` environment variable. User email addresses are excluded from metric labels to avoid PII exposure.

### Rate Limiting

An IP-based rate limiter protects admin endpoints, `/metrics`, and webhook endpoints (60 requests per IP per 60-second window).

### Security Headers

All responses include: `X-Content-Type-Options`, `X-Frame-Options`, `Strict-Transport-Security`, `Referrer-Policy`, `X-XSS-Protection`, and `Permissions-Policy`.

### Required Environment Variables (Security)

| Variable | Purpose |
|----------|---------|
| `ORCHESTRA_ADMIN_KEY` | Admin API authentication |
| `PROMETHEUS_METRICS_TOKEN` | Metrics endpoint authentication |
| `CLOUD_SCHEDULER_SERVICE_ACCOUNT` | (Optional) Service account email for OIDC-based scheduler auth |

### GCP Infrastructure (not tracked in code)

The following infrastructure settings are configured directly in GCP (`saas-368716`):

- **Cloud SQL**: SSL required on all instances (`prod-ssd`, `staging-ssd`, `dev`). `prod-ssd` uses Auth Proxy enforcement. The `dev` instance requires `sslMode: ENCRYPTED_ONLY`.
- **Secret Manager**: `EVAL_SERVER_PASSWORD` and `EVAL_SERVER_URL` are stored in Secret Manager and mounted into the Cloud Run service as secrets (not plaintext environment variables).
- **Firewall rules**: Elasticsearch restricted to VPC internal (`10.0.0.0/8`); Grafana/Loki/Tempo restricted to VPC internal; SSH/RDP restricted to IAP tunnel range (`35.235.240.0/20`); `allow-omniparser-port-8000` restricted to IAP range.
- **Storage buckets**: `publicAccessPrevention` enforced on all 18 buckets. No `allUsers` or `allAuthenticatedUsers` bindings on any bucket.
- **Cloud Run ingress**: Currently `all` (default). Restricting to `internal-and-cloud-load-balancing` requires migrating from Cloud Run custom domain mappings to a proper Google Cloud Load Balancer with serverless NEGs first — custom domain mapping traffic is classified as external and gets rejected with 404.
- **Prometheus VM**: Network tags limited to `monitoring` only (no `http-server`/`https-server`). Grafana, Loki, Tempo, and Prometheus are not directly reachable from the internet; access is via IAP or VPN.
- **Cloud Armor**: OWASP WAF rules (SQLi, XSS, LFI, RFI, RCE) on all security policies.
- **Cloud Scheduler**: All scheduler jobs in both `saas-368716` and `responsive-city-458413-a2` include `Authorization` headers.

### GitHub Repository Settings (not tracked in code)

- **Branch protection** on `main`: Requires 1 approving pull request review. Force pushes and branch deletions are blocked.
- **Dependabot**: Vulnerability alerts and automated security fixes are enabled.

## Docs and other READMEs

- [API Endpoints README](./orchestra/web/api/README.md): How to add new endpoints to the orchestra API, How to secure the endpoints.
- [Database README](./orchestra/db/README.md): Migrations, Google Cloud Infrastructure, Syncying staging DB.
- TODO: [Secrets and Environment Variables](#secrets--environment-variables)
- [Running the tests](#running-the-tests)
- [Running orchestra](#running-orchestra)
- TODO: Tests (unit, integration, load testing, manual)
- [Observability](./orchestra/observability/README.md): Monitoring, logging, and tracing setup.
- [CI/CD](./.github/workflows/README.md)

## Project structure

```bash
$ tree "orchestra"
orchestra
├── __main__.py  # Startup script. Starts uvicorn.
├── conftest.py  # Fixtures for all tests.
├── settings.py  # Main configuration settings for project.
├── tests  # Tests for project.
├── db  # module contains db configurations
│   ├── migrations  # Files related to alembic migrations.
│   ├── dao  # Data Access Objects. Contains different classes to interact with database.
│   └── models  # Package contains different models for ORMs.
└── web  # Package contains web server. Handlers, startup config.
    ├── api  # Package with all handlers.
    │   └── dependencies.py  # Contains utilities and helpers for v0/router.
    │   └── router.py  # Main router.
    ├── application.py  # FastAPI application configuration.
    └── lifetime.py  # Contains actions to perform on startup and shutdown.
```

## Poetry

This project uses poetry to manage dependencies.
To install dependencies:

```bash
poetry install
```

For development, you can activate the poetry virtual environment by:

```bash
poetry shell
```

## Pre-commit

To install pre-commit simply run inside the shell:
```bash
pre-commit install
```

## Secrets / Environment Variables

TODO: Tool to fetch them from GCP

VSCode will load the .env file by default, but take into account that you might need to configure your IDE to load the variables.

## Running the tests

To run the orchestra test suite, you will need the poetry environment and a PostgreSQL server running with the `pgvector` extension installed. The tests and vector functions require `pgvector`.

Recommended (pgvector-enabled Postgres container):

```bash
docker run --name orchestra-db -p 5432:5432 \
  -e POSTGRES_PASSWORD=orchestra -e POSTGRES_USER=orchestra -e POSTGRES_DB=orchestra \
  pgvector/pgvector:pg15
```

Once the database server is running, install dependencies (including dev) and run the tests using the poetry environment. Take into account that some tests will require **secrets and environment variables**.

```bash
poetry install --with dev
poetry run pytest -vv .
```

If you see an error like `extension "vector" is not available`, your Postgres instance lacks pgvector. Use the image above or install pgvector in your local Postgres and run `CREATE EXTENSION IF NOT EXISTS vector;` in the target database.

## Running orchestra

To run the orchestra service locally, you will need a database with valid data, the corresponding secrets/environment variables, and the poetry environment.

If you already have a docker container running Postgres with pgvector you won't need to create a new image. Otherwise:

```bash
docker run --name orchestra-db -p 5432:5432 \
  -e POSTGRES_PASSWORD=orchestra -e POSTGRES_USER=orchestra -e POSTGRES_DB=orchestra \
  pgvector/pgvector:pg15
```

Everytime you create a new container, you should run migrations:

```bash
alembic upgrade "head"
```

Now, connect to the PSQL database (password=`orchestra`):

```bash
psql -h localhost -U orchestra -d orchestra
```

Your DB should now be fully functional! Now, you should be able to see all the tables (e.g. `\dt`).

To run the service, you can do `poetry run python -m orchestra` but you won't be able to debug the service.

To run orchestra in debug mode (in VSCode / Codespaces), your `launch.json` file should look something like this:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: FastAPI",
            "type": "python",
            "request": "launch",
            "module": "uvicorn",
            "args": [
                "orchestra.web.application:get_app",
                "--reload"
            ],
            "jinja": true,
            "justMyCode": true
        }
    ]
}
```

Once the service is running, you can send requests to http://127.0.0.1:8000/v0

## Observability

Orchestra uses a comprehensive observability stack for monitoring, logging, and tracing.

### Local Development Setup

For local development, you can run the observability stack using Docker Compose:

```bash
docker-compose -f orchestra/observability/docker-compose.observability.yml up -d
```

This will start Prometheus, Loki, Tempo, and Grafana containers locally. You can access Grafana at http://localhost:3000.

### Production Setup

In production, we have a dedicated monitoring infrastructure with:

- **Grafana Dashboard**: https://grafana.saas.unify.ai
  - Secured with Google OAuth authentication
  - Only accessible to users with @unify.ai email addresses

- **Monitoring Components**:
  - Prometheus for metrics collection
  - Loki for log aggregation
  - Tempo for distributed tracing

For more details on how to use the observability stack, refer to the [Observability README](./orchestra/observability/README.md).

## Configuration

This application can be configured with environment variables.

You can create `.env` file in the root directory and place all
environment variables here.

All environment variables should start with "ORCHESTRA_" prefix.

For example if you see in your "orchestra/settings.py" a variable named like
`random_parameter`, you should provide the "ORCHESTRA_RANDOM_PARAMETER"
variable to configure the value. This behaviour can be changed by overriding `env_prefix` property
in `orchestra.settings.Settings.Config`.

An example of .env file:
```bash
ORCHESTRA_RELOAD="True"
ORCHESTRA_PORT="8000"
ORCHESTRA_ENVIRONMENT="dev"
```

You can read more about BaseSettings class here: https://pydantic-docs.helpmanual.io/usage/settings/

## Pytest async configuration

We use pytest-asyncio in STRICT mode. The default fixture loop scope is already set to `function` in `pyproject.toml`, so you should not see related deprecation warnings.

## OpenTelemetry

If you want to start your project with OpenTelemetry collector
you can add `-f ./deploy/docker-compose.otlp.yml` to your docker command.

Like this:

```bash
docker-compose -f deploy/docker-compose.yml -f deploy/docker-compose.otlp.yml --project-directory . up
```

This command will start OpenTelemetry collector and jaeger.
After sending a requests you can see traces in jaeger's UI
at http://localhost:16686/.

This docker configuration is not supposed to be used in production.
It's only for demo purpose.

You can read more about OpenTelemetry here: https://opentelemetry.io/
