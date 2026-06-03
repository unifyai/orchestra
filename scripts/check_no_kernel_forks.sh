#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

forbidden_paths=(
    "orchestra/db/dao/log_event_dao.py"
    "orchestra/web/api/log/schema.py"
    "orchestra/web/api/log/python2SQL"
    "orchestra/web/api/log/utils"
)

violations=()
for relpath in "${forbidden_paths[@]}"; do
    if [[ -e "$ROOT/$relpath" ]]; then
        violations+=("$relpath")
    fi
done

if (( ${#violations[@]} > 0 )); then
    echo "FAIL: platform contains kernel-owned fork paths:" >&2
    printf '  %s\n' "${violations[@]}" >&2
    exit 1
fi

echo "OK: no platform kernel forks found."
