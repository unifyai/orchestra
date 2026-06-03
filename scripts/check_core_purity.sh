#!/usr/bin/env bash
# CI invariant: orchestra_core/ never imports from orchestra (the platform)
# and never references platform-only models. The dependency graph is one-way:
#
#     orchestra (platform) ---> orchestra_core
#
# This is the single most important architectural property of the phase-1
# split. Violations let private code leak into the future public repo.
set -euo pipefail

CORE_DIR="${ORCHESTRA_CORE_DIR:-}"
if [[ -z "$CORE_DIR" ]]; then
    CORE_DIR="$(
        python - <<'PY'
import pathlib
import orchestra_core

print(pathlib.Path(orchestra_core.__file__).resolve().parent)
PY
    )"
fi

if [[ ! -d "$CORE_DIR" ]]; then
    echo "FAIL: orchestra_core package directory not found: $CORE_DIR" >&2
    exit 1
fi

echo "Checking orchestra_core/ purity..."

# 1. Forbid TOP-LEVEL imports of `orchestra.*` (the platform package) from
#    inside orchestra_core/. Top-level == start of line, no leading
#    whitespace; these execute unconditionally on module load and are the
#    strict architectural constraint. Deferred imports inside function
#    bodies (for optional platform-only enrichment) are deliberately
#    permitted and runtime-guarded.
violations=$(
    grep -rn -E '^(from|import) orchestra\.' "$CORE_DIR" \
        --include='*.py' \
        | grep -v '^[^:]*:[0-9]*:from orchestra_core' \
        | grep -v '^[^:]*:[0-9]*:import orchestra_core' \
        || true
)
if [[ -n "$violations" ]]; then
    echo "FAIL: orchestra_core/ has top-level imports from orchestra/ (platform):" >&2
    echo "$violations" >&2
    exit 1
fi

# 2. Forbid imports of platform model classes via the orchestra_models
#    facade. The kernel imports kernel models from orchestra_core.db.models
#    directly; an orchestra_models import means the file is reaching for
#    something that lives in the platform.
facade_violations=$(
    grep -rn -E '^(from|import) orchestra\.db\.models\.orchestra_models' \
        "$CORE_DIR" --include='*.py' || true
)
if [[ -n "$facade_violations" ]]; then
    echo "FAIL: orchestra_core/ imports from the platform model facade:" >&2
    echo "$facade_violations" >&2
    exit 1
fi

echo "OK: orchestra_core/ is clean."
