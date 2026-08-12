FROM python:3.12-slim-bookworm as prod
RUN apt-get update && apt-get install -y \
  gcc \
  && rm -rf /var/lib/apt/lists/*

# Install curl
RUN apt-get update && apt-get install -y curl

# Download and install Google Cloud SDK
RUN curl https://sdk.cloud.google.com > install.sh
RUN bash install.sh --disable-prompts

# Standalone uv binary: not a site-package, so syncing into /usr/local
# cannot uninstall the installer itself.
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /usr/local/bin/

# Install into the image's interpreter instead of a per-project .venv.
ENV UV_PROJECT_ENVIRONMENT=/usr/local

# Copying requirements of a project
COPY pyproject.toml uv.lock /app/src/
WORKDIR /app/src

# Installing requirements
RUN uv sync --frozen --no-dev --no-install-project

# Removing gcc
RUN apt-get purge -y \
  gcc \
  && rm -rf /var/lib/apt/lists/*

# Copying actual application
COPY . /app/src/
RUN uv sync --frozen --no-dev

CMD if [ "$ON_PREM" = "true" ]; then \
  echo "Running on prem"; \
  bash ./on_prem_setup.sh; \
  else \
  echo "Running on cloud"; \
  /usr/local/bin/python -m orchestra; \
  fi

FROM prod as dev

RUN uv sync --frozen
