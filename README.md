# orchestra


## Poetry

This project uses poetry. It's a modern dependency management tool.
To run the project use this set of commands:

```bash
poetry install
poetry run python -m orchestra
```

This will start the server on the configured host.

You can find swagger documentation at `/v0/docs`.

For development, you can activate the poetry virtual environment by:

```bash
poetry shell
```

## Docker

You can start the project with docker using this command:

```bash
docker-compose -f deploy/docker-compose.yml --project-directory . up --build
```

If you want to develop in docker with autoreload add `-f deploy/docker-compose.dev.yml` to your docker command.
Like this:

```bash
docker-compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml --project-directory . up --build
```

This command exposes the web application on port 8000, mounts current directory and enables autoreload.

But you have to rebuild image every time you modify `poetry.lock` or `pyproject.toml` with this command:

```bash
docker-compose -f deploy/docker-compose.yml --project-directory . build
```

### Remove all old artifacts in local
```bash
docker-compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml --project-directory . down --volumes --remove-orphans
docker rmi $(docker images -q)
docker volume prune
```
It is recommended to use the poetry commands for local development as docker does not play well with the database.

## Project structure

```bash
$ tree "orchestra"
orchestra
├── conftest.py  # Fixtures for all tests.
├── db  # module contains db configurations
│   ├── dao  # Data Access Objects. Contains different classes to interact with database.
│   └── models  # Package contains different models for ORMs.
├── __main__.py  # Startup script. Starts uvicorn.
├── services  # Package for different external services such as rabbit or redis etc.
├── settings.py  # Main configuration settings for project.
├── static  # Static content.
├── tests  # Tests for project.
└── web  # Package contains web server. Handlers, startup config.
    ├── api  # Package with all handlers.
    │   └── dependencies.py  # Contains utilities and helpers for v0/router.
    │   └── router.py  # Main router.
    ├── application.py  # FastAPI application configuration.
    └── lifetime.py  # Contains actions to perform on startup and shutdown.
```

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

## Pre-commit

To install pre-commit simply run inside the shell:
```bash
pre-commit install
```

Run tests before pushing them
```bash
pre-commit run -a
poetry run mypy .
```

## Migrations

If you want to migrate your database, you should run following commands:
```bash
# To run all migrations until the migration with revision_id.
alembic upgrade "<revision_id>"

# To perform all pending migrations.
alembic upgrade "head"
```

### Reverting migrations

If you want to revert migrations, you should run:
```bash
# revert all migrations up to: revision_id.
alembic downgrade <revision_id>

# Revert everything.
alembic downgrade base
```

### Migration generation

To generate migrations you should run:
```bash
# For automatic change detection.
alembic revision --autogenerate

# For empty file generation.
alembic revision
```


## Endpoint Protection

### User-Facing

To enable user API key authentication on endpoints, you should add the following
in the `orchestra/web/api/router.py` file:

```python
api_router.include_router(
    ...,
    dependencies=API_KEY_AUTH,
)
```

For example, this will protect all endpoints in the `/dummy` router:
```python
api_router.include_router(
    dummy.router,
    prefix="/dummy",
    tags=["dummy"],
    dependencies=API_KEY_AUTH,
)
```

### Admin Authentication

To enable admin-only API key authentication on endpoints, you should add the following
in the `orchestra/web/api/router.py` file:

```python
api_router.include_router(
    ...,
    dependencies=ADMIN_AUTH,
)
```

For example, this will protect all endpoints in the `/dummy` router
to allow admin-only access:
```python
api_router.include_router(
    dummy.router,
    prefix="/dummy",
    tags=["dummy"],
    dependencies=ADMIN_AUTH,
)
```

For testing purposes, an example is to add `ORCHESTRA_ADMIN_KEY="testing-123"` to the `.env` file for verifying the behaviour of the admin key authentication.


## Running tests

If you want to run it in docker, simply run:

```bash
docker-compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml --project-directory . run --build --rm api pytest -vv .
docker-compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml --project-directory . down
```

For running tests on your local machine.
1. you need to start a database.

I prefer doing it with docker:
```
docker run -p "5432:5432" -e "POSTGRES_PASSWORD=orchestra" -e "POSTGRES_USER=orchestra" -e "POSTGRES_DB=orchestra" postgres:13.8-bullseye
```

2. Run the pytest.
```bash
pytest -vv .
```
## Setting up the database locally
### Getting local dump file
1. To setup the database locally, you need to create a dump file of the staging database. To do so, you can either run the following commands:
    - Connect to the sql database using gcloud:
        ```bash
        gcloud sql connect staging
        ```
    - After connecting to staging, run the following command on your terminal. This will create a dump file.
        ```bash
        pg_dump -h 34.141.85.117 -p 5432 -U postgres orchestra > orchestra.sql
        ```
    OR
    - Use GCP UI to pg_dump the data through the [export page here](https://console.cloud.google.com/sql/instances/dev/export?project=saas-368716) into [temp_file_holder](https://console.cloud.google.com/storage/browser/temp_file_holder;tab=objects?forceOnBucketsSortingFiltering=true&project=saas-368716&prefix=&forceOnObjectsSortingFiltering=false) bucket and then download from there.
2. Now, connect to psql and run the following command to populate your local orchestra database. Note: this step assumes you've database setup and running.
    ```bash
    PGPASSWORD=orchestra psql -h localhost -U orchestra -d orchestra
    \i <path to orchestra.sql>
    \q
    ```

### Setting up the database in the docker container
This way you can configure your database that is spun up using the docker compose.
```bash
docker exec -it <postgres:13.8-bullseye_container_id>/bin/bash
```

Now, connect to psql and run the following command to populate your local orchestra database
```bash
psql -h 127.0.0.1  -p 5432 -U orchestra -d orchestra
```

```sql
\connect orchestra;
\i <snapshot_name>.sql
```

### Setting up the local database
To populate database for the local orchestra, run the following commands
```bash
poetry run python -m orchestra
docker run -p "5432:5432" -e "POSTGRES_PASSWORD=orchestra" -e "POSTGRES_USER=orchestra" -e "POSTGRES_DB=orchestra" postgres:13.8-bullseye
alembic upgrade head
```

Now, connect to psql and run the following command to populate your local orchestra database
```bash
psql -h 127.0.0.1  -p 5432 -U orchestra -d orchestra
```

```sql
\connect orchestra;
\i <snapshot_name>.sql
```

## Debugging in vscode

To run the debugger you will need a valid connection to a db. To run `orchestra` in debug model, your `launch.json` file should look something like this:

```python
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

## Staging environment

The `staging` branch has CI/CD already setup. All changes there will reflect to https://orchestra-staging-lz5fmz6i7q-ew.a.run.app.

## Telemetry

For instructions to access the telemetry dashboard and how it's setup, please see [Telemetry.md](./Telemetry.md)
