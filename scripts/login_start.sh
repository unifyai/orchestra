#!/usr/bin/env bash
# Entry point for the ai.unify.orchestra-local LaunchAgent (RunAtLoad only:
# no KeepAlive, so a crash or `local.sh stop` keeps the server stopped until
# the next login or an explicit start).
#
# Docker Desktop races login items on macOS, so wait for the daemon before
# handing off to local.sh, which owns the DB container, migrations, and the
# server launch.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

deadline=$((SECONDS + 300))
until docker info >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "Docker daemon not reachable after 5 minutes; not starting Orchestra." >&2
    exit 1
  fi
  sleep 5
done

exec "$ROOT/scripts/local.sh" start
