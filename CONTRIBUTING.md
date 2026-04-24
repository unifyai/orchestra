# Contributing to Orchestra

## Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/)
- Docker or another PostgreSQL instance with the `pgvector` extension

## Setup

1. Clone the repository and install dependencies:

```bash
git clone https://github.com/unifyai/orchestra.git
cd orchestra
poetry install --with dev
```

2. Copy the local-development env template:

```bash
cp .env.example .env
```

Use `.env.advanced.example` instead if you need the broader hosted/platform
configuration surface for storage, OAuth, billing, media, or scheduler flows.

3. Start a local pgvector-enabled Postgres:

```bash
docker run --name orchestra-db -p 5432:5432 \
  -e POSTGRES_PASSWORD=orchestra -e POSTGRES_USER=orchestra -e POSTGRES_DB=orchestra \
  pgvector/pgvector:pg15
```

4. Run migrations and start the API:

```bash
poetry run alembic upgrade head
poetry run python -m orchestra
```

## Running tests

Most tests require PostgreSQL with `pgvector` available. Once the database is
running and your environment is configured:

```bash
poetry run pytest -vv .
```

Some integration paths also require additional secrets and managed
infrastructure. External contributors should expect the full CI matrix to be
maintainer-controlled for those cases.

## Code style

Install pre-commit hooks:

```bash
poetry run pre-commit install
```

Run the default local checks manually:

```bash
poetry run pre-commit run --all-files
```

## Pull requests

- Open PRs against the `staging` branch.
- Keep changes focused and reviewable.
- Run the relevant tests for the area you changed.

## Questions

Open a GitHub issue or discussion with reproduction steps, environment details,
and the behavior you expected to see.
