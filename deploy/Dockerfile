FROM python:3.11.4-slim-bullseye as prod

# Install curl
RUN apt-get update && apt-get install -y curl \
    && apt-get clean -y && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=off \
  # Poetry's configuration:
  POETRY_NO_INTERACTION=1 \
  POETRY_VIRTUALENVS_CREATE=false \
  POETRY_CACHE_DIR='/var/cache/pypoetry' \
  POETRY_HOME='/usr/local' \
  POETRY_VERSION=1.7.1

RUN curl -sSL https://install.python-poetry.org | python3 -

# Configuring poetry
RUN poetry config virtualenvs.create false

# Copying requirements of a project
COPY pyproject.toml poetry.lock /app/src/
WORKDIR /app/src

# Installing requirements
RUN poetry install --only main

# Copying actual application
COPY . /app/src/
RUN poetry install --only main

CMD ["/usr/local/bin/python", "-m", "orchestra"]

FROM prod as dev

RUN poetry install
