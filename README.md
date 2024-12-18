# orchestra

This repo includes the code for orchestra, the server in charge of the model hub api and the benchmarks.

- Orchestra Production API URL: https://api.unify.ai/v0
- Orchestra Staging API URL: https://orchestra-staging-lz5fmz6i7q-ew.a.run.app/v0

## Docs and other READMEs

- [API Endpoints README](./orchestra/web/api/README.md): How to add new endpoints to the orchestra API, How to secure the endpoints.
- [Database README](./orchestra/db/README.md): Migrations, Google Cloud Infrastructure, Syncying staging DB.
- TODO: [Secrets and Environment Variables](#secrets--environment-variables)
- [Running the tests](#running-the-tests)
- [Running orchestra](#running-orchestra)
- TODO: Tests (unit, integration, load testing, manual)
- TODO: Adding model, providers, and model endpoints
- TODO: [Telemetry](./Telemetry.md)
- [Google Cloud](./GCP.md): GCP services being used.
- [CI/CD](./.github/workflows/README.md)
- [Pub/Sub](./PubSub.md): How to set up and interact with Pub/Sub message queues.

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
│   ├── dao  # Data Access Objects. Contains different classes to interact with database.
│   └── models  # Package contains different models for ORMs.
└── web  # Package contains web server. Handlers, startup config.
    ├── api  # Package with all handlers.
    │   └── dependencies.py  # Contains utilities and helpers for v0/router.
    │   └── router.py  # Main router.
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

TODO: Lorem ipsum
TODO: Tool to fetch them from GCP

VSCode will load the .env file by default, but take into account that you might need to configure your IDE to load the variables.

## Running the tests

To run the orchestra test suite, you will need the poetry environment and a PSQL server running. Both in codespaces and locally, the easiest way to do this is by creating a PSQL image from the official container. You **don't** need to create a database or populate with data, the tests procedures will do it.

```bash
docker run -p "5432:5432" -e "POSTGRES_PASSWORD=orchestra" -e "POSTGRES_USER=orchestra" -e "POSTGRES_DB=orchestra" postgres:15.2-bullseye
```

Once the database server is running, you can run the tests using the poetry environment. Take into account that some tests will require **secrets and environment variables**.

```bash
pytest -vv .
```

## Running orchestra

To run the orchestra service locally, you will need a database with valid data, the corresponding secrets/environment variables, and the poetry environment.

If you already have a docker container running PSQL you won't need to create a new image. Otherwise:

```bash
docker run -p "5432:5432" -e "POSTGRES_PASSWORD=orchestra" -e "POSTGRES_USER=orchestra" -e "POSTGRES_DB=orchestra" postgres:15.2-bullseye
```

Everytime you create a new container, you should run migrations:

```bash
alembic upgrade "head"
```

In order to add your user information to the db, you need
1. user_id: you can get your user id by making a request to the [/credits](https://docs.unify.ai/api-reference/credits/get_credits) endpoint (the "id" key in the response contains your user_id)
2. api_key: you can get your api key through the console
3. email_id: your email id used for logging in to this account

The basic data containing the models, providers and endpoints is stored in a GCP bucket that will be used by the script.
Now, you should run the script:

```bash
python add_latest_endpoint_data.py <user_id> <email_id> <api_key>
```

which should populate the tables with the necessary data to get started.

Now, connect to the PSQL database (password=`orchestra`):

```bash
psql -h localhost -U orchestra -d orchestra
```

Your DB should now be fully functional! Now, you should be able to see all the tables (e.g. `\dt`).

To run the service, you can do `poetry run python -m orchestra` but you won't be able to debug the service.

To run orchestra in debug mode (in VSCode / Codespaces), your `launch.json` file should look something like this:

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

Once the service is running, you can send requests to http://127.0.0.1:8000/v0

-----------------------

TODO: Clean below

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
