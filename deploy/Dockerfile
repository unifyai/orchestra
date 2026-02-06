FROM python:3.12-slim-bookworm AS prod
RUN apt-get update && apt-get install -y \
  gcc \
  g++ \
  git \
  curl \
  && rm -rf /var/lib/apt/lists/*

# Download and install Google Cloud SDK
RUN curl https://sdk.cloud.google.com > install.sh
RUN bash install.sh --disable-prompts

RUN pip install poetry>=2.1.0

# Configuring poetry
RUN poetry config virtualenvs.create false
RUN poetry config cache-dir /tmp/poetry-cache

# Copy dependency repos first (from build context root)
# These are cloned as siblings in Cloud Build
COPY unify/ /app/unify/
COPY unillm/ /app/unillm/

# Copying requirements of a project (from orchestra subdirectory)
COPY orchestra/pyproject.toml orchestra/poetry.lock /app/src/
WORKDIR /app/src

# Installing requirements (../unify resolves to /app/unify/)
RUN poetry install --only main --no-root

# Removing gcc
RUN apt-get purge -y \
  gcc \
  && rm -rf /var/lib/apt/lists/*

# Copying actual application
COPY orchestra/ /app/src/
RUN poetry install --only main

CMD if [ "$ON_PREM" = "true" ]; then \
  echo "Running on prem"; \
  bash ./on_prem_setup.sh; \
  else \
  echo "Running on cloud"; \
  /usr/local/bin/python -m orchestra; \
  fi

FROM prod AS dev

RUN poetry install
