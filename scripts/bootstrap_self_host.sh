#!/usr/bin/env bash
# Bootstrap self-host platform defaults. The owner is created only through Console signup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

cd "$ORCHESTRA_REPO_PATH"

if [[ -x "$ORCHESTRA_REPO_PATH/.venv/bin/python" ]]; then
  PYTHON="$ORCHESTRA_REPO_PATH/.venv/bin/python"
elif command -v uv &>/dev/null; then
  PYTHON="uv run --project $ORCHESTRA_REPO_PATH python"
else
  echo "[ERROR] Orchestra Python environment not found" >&2
  exit 1
fi

# The local database is rarely on 5432: local.sh publishes the container on a
# high port to stay clear of a system Postgres, and resolves it per run. A
# hardcoded 5432 default here does not fail — it finds whatever else is
# listening and bootstraps *that*, silently leaving the real database without
# its system projects. Mirror local.sh's detection and only fall back to 5432
# when there is no container to ask.
resolve_db_port() {
  local container="${ORCHESTRA_DB_CONTAINER:-orchestra-local-db}"
  local mapped=""
  if command -v docker >/dev/null 2>&1; then
    mapped="$(docker port "$container" 5432/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    if [[ -z "$mapped" ]]; then
      mapped="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' "$container" 2>/dev/null || true)"
    fi
  fi
  printf '%s' "${mapped:-5432}"
}

export SELF_HOST=1
export ORCHESTRA_DB_HOST="${ORCHESTRA_DB_HOST:-localhost}"
export ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-$(resolve_db_port)}"
export ORCHESTRA_DB_USER="${ORCHESTRA_DB_USER:-orchestra}"
export ORCHESTRA_DB_PASS="${ORCHESTRA_DB_PASS:-orchestra}"
export ORCHESTRA_DB_BASE="${ORCHESTRA_DB_BASE:-orchestra}"
export SELF_HOST_BOOTSTRAP_OUTPUT="/tmp/self-host-bootstrap.json"

# Name the target: bootstrapping the wrong database is otherwise indistinguishable
# from success.
echo "[INFO] Bootstrapping ${ORCHESTRA_DB_BASE} at ${ORCHESTRA_DB_HOST}:${ORCHESTRA_DB_PORT}" >&2

$PYTHON scripts/bootstrap_self_host.py
