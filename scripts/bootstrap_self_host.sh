#!/usr/bin/env bash
# Bootstrap self-host platform defaults. The owner is created only through Console signup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

cd "$ORCHESTRA_REPO_PATH"

if [[ -x "$ORCHESTRA_REPO_PATH/.venv/bin/python" ]]; then
  PYTHON="$ORCHESTRA_REPO_PATH/.venv/bin/python"
elif command -v poetry &>/dev/null; then
  PYTHON="poetry run python"
else
  echo "[ERROR] Orchestra Python environment not found" >&2
  exit 1
fi

export SELF_HOST=1
export ORCHESTRA_DB_HOST="${ORCHESTRA_DB_HOST:-localhost}"
export ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-5432}"
export ORCHESTRA_DB_USER="${ORCHESTRA_DB_USER:-orchestra}"
export ORCHESTRA_DB_PASS="${ORCHESTRA_DB_PASS:-orchestra}"
export ORCHESTRA_DB_BASE="${ORCHESTRA_DB_BASE:-orchestra}"
export SELF_HOST_BOOTSTRAP_OUTPUT="/tmp/self-host-bootstrap.json"

$PYTHON scripts/bootstrap_self_host.py
