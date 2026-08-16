#!/usr/bin/env bash
# =============================================================================
# local_orchestra.sh - Manage a local Orchestra instance for development/testing
# =============================================================================
#
# This script starts a fully local Orchestra deployment using:
#   1. A Docker container running PostgreSQL with pgvector
#   2. The Orchestra FastAPI server
#
# This eliminates network latency and staging server bottlenecks during testing.
#
# Scope: this is an INTERNAL dev/test harness for Orchestra alone. To run the
# whole product locally (Orchestra + Unity gateway + Console + Coordinator), use
# `unity stack up` from the unity repo — it invokes this script for you.
#
# Usage:
#   ./local_orchestra.sh start    # Start and wait for ready (preserves data)
#   ./local_orchestra.sh stop     # Stop local orchestra (preserves data)
#   ./local_orchestra.sh restart  # Stop then start (preserves data)
#   ./local_orchestra.sh purge    # Destroy container + named volume (wipes data)
#   ./local_orchestra.sh check    # Check if already running
#   ./local_orchestra.sh status   # Show status
#
# Environment:
#   ORCHESTRA_REPO_PATH     Path to orchestra repo (default: auto-detect from script location)
#   ORCHESTRA_PORT          FastAPI port (default: 8000)
#   ORCHESTRA_DB_PORT       PostgreSQL port (default: 5432)
#   ORCHESTRA_PREFIX        Prefix for container/PID names (default: "orchestra")
#   ORCHESTRA_LOG_DIR       Directory for orchestra logs (optional)
#   ORCHESTRA_OTEL_LOG_DIR  Directory for OpenTelemetry traces (optional)
#   ORCHESTRA_WORKERS       Number of uvicorn workers (default: auto-detect from CPU cores)
#   ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS  Shutdown after N seconds of no requests (default: 600)
#
# Test user (seeded by default for local development):
#   ORCHESTRA_TEST_USER_ID  Test user ID (default: "test-user-001")
#   ORCHESTRA_TEST_EMAIL    Test user email (default: "test@debug.local")
#   ORCHESTRA_SKIP_TEST_USER  Set to 1 to skip local test-user seeding
#   UNIFY_KEY               API key for test user (default: "local-test-api-key")
#   ORCHESTRA_ADMIN_KEY     Admin bearer (default: "local-admin-key"; must differ from UNIFY_KEY)
#
# On success, exports:
#   UNIFY_BASE_URL=http://127.0.0.1:8000/v0
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

# Resolve script directory and orchestra repo path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

# Configurable prefix for container/PID names (allows multiple instances)
ORCHESTRA_PREFIX="${ORCHESTRA_PREFIX:-orchestra}"

# Ports
ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}"
_ORCHESTRA_DB_PORT_FROM_ENV="${ORCHESTRA_DB_PORT:-}"
unset ORCHESTRA_DB_PORT

# Inactivity timeout (seconds) - server shuts down after this period of no requests.
# Default 24h: long LLM/pytest suites have multi-minute gaps between Orchestra calls
# (UniLLM credits/deduct only); the old 600s default caused mid-run shutdowns.
ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS="${ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS:-86400}"

# Local development seeds a test account unless a caller explicitly opts out.
ORCHESTRA_SKIP_TEST_USER="${ORCHESTRA_SKIP_TEST_USER:-0}"

# Derived names using prefix
ORCHESTRA_DB_CONTAINER="${ORCHESTRA_PREFIX}-local-db"
ORCHESTRA_DB_VOLUME="${ORCHESTRA_PREFIX}-local-db-data"
ORCHESTRA_SERVER_PIDFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.pid"
ORCHESTRA_SERVER_LOGFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.log"
ORCHESTRA_SERVER_CONFIGFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.config"

_db_container_host_port() {
  local container="${1:-$ORCHESTRA_DB_CONTAINER}"
  local mapped=""
  mapped="$(docker port "$container" 5432/tcp 2>/dev/null | head -1 | sed 's/.*://')"
  if [[ -z "$mapped" ]]; then
    mapped="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' "$container" 2>/dev/null || true)"
  fi
  printf '%s' "$mapped"
}

read_orchestra_db_port_from_config() {
  if [[ ! -f "$ORCHESTRA_SERVER_CONFIGFILE" ]]; then
    return 1
  fi
  grep "^ORCHESTRA_DB_PORT=" "$ORCHESTRA_SERVER_CONFIGFILE" 2>/dev/null | cut -d= -f2- | head -1
}

resolve_orchestra_db_port() {
  if [[ -n "${_ORCHESTRA_DB_PORT_FROM_ENV:-}" ]]; then
    printf '%s' "$_ORCHESTRA_DB_PORT_FROM_ENV"
    return 0
  fi

  local from_config mapped=""
  from_config="$(read_orchestra_db_port_from_config 2>/dev/null || true)"
  if [[ -n "$from_config" ]]; then
    printf '%s' "$from_config"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    mapped="$(_db_container_host_port "$ORCHESTRA_DB_CONTAINER")"
    if [[ -n "$mapped" ]]; then
      printf '%s' "$mapped"
      return 0
    fi
  fi

  printf '5432'
}

ORCHESTRA_DB_PORT="$(resolve_orchestra_db_port)"
unset _ORCHESTRA_DB_PORT_FROM_ENV

# URLs
LOCAL_ORCHESTRA_URL="http://127.0.0.1:${ORCHESTRA_PORT}/v0"
STAGING_URL="https://internal.example.com/v0"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

full_stack_state_file() {
  printf '%s/full-stack-state.json' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

port_is_listening() {
  local port="$1"
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed -n '2p' | grep -q .
}

full_stack_source_is_active() {
  local state_file
  state_file="$(full_stack_state_file)"
  if [[ -f "$state_file" ]]; then
    python3 - "$state_file" <<'PY' >/dev/null || return 1
import json
import sys
with open(sys.argv[1], encoding="utf-8") as fh:
    mode = json.load(fh).get("mode")
raise SystemExit(0 if not mode or mode == "source" else 1)
PY
  fi
  port_is_listening 8000 && port_is_listening 8001
}

refuse_isolated_when_full_stack_active() {
  local action="$1"
  if [[ "${ORCHESTRA_ALLOW_ISOLATED:-0}" == "1" || -n "${UNIFY_STACK_ORCHESTRATOR:-}" ]]; then
    return 0
  fi
  if full_stack_source_is_active; then
    log_error "Refusing isolated Orchestra $action while the full local Unity stack is active."
    log_info "Use unity-deploy/selfhost/stack.sh status or repair-console instead."
    log_info "Override only for intentionally isolated Orchestra work:"
    log_info "  ORCHESTRA_ALLOW_ISOLATED=1 $0 $action"
    return 1
  fi
}

# =============================================================================
# Prerequisite Checks
# =============================================================================

start_docker_daemon() {
  log_info "Attempting to start Docker daemon..."

  if [[ "$(uname)" == "Darwin" ]]; then
    # macOS: Start Docker Desktop
    if [[ -d "/Applications/Docker.app" ]]; then
      open -a Docker
      log_info "Started Docker Desktop, waiting for daemon..."
    else
      log_error "Docker Desktop not found at /Applications/Docker.app"
      return 1
    fi
  else
    # Linux: Try systemctl first, then service
    if command -v systemctl &>/dev/null; then
      if sudo systemctl start docker 2>/dev/null; then
        log_info "Started Docker via systemctl"
      else
        log_error "Failed to start Docker via systemctl"
        return 1
      fi
    elif command -v service &>/dev/null; then
      if sudo service docker start 2>/dev/null; then
        log_info "Started Docker via service"
      else
        log_error "Failed to start Docker via service"
        return 1
      fi
    else
      log_error "No supported method to start Docker daemon"
      return 1
    fi
  fi

  # Wait for Docker daemon to be ready
  local max_attempts=60
  local attempt=0
  while (( attempt < max_attempts )); do
    if docker info &>/dev/null; then
      log_success "Docker daemon is now running"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "Docker daemon failed to start within 60 seconds"
  return 1
}

check_docker() {
  if ! command -v docker &>/dev/null; then
    log_error "Docker is not installed"
    return 1
  fi

  if ! docker info &>/dev/null; then
    log_warn "Docker daemon is not running"
    if ! start_docker_daemon; then
      log_error "Could not start Docker daemon"
      return 1
    fi
  fi

  log_success "Docker is available"
  return 0
}

check_orchestra_repo() {
  local repo_path="$1"

  if [[ ! -d "$repo_path" ]]; then
    log_error "Orchestra repo not found at: $repo_path"
    return 1
  fi

  if [[ ! -f "$repo_path/pyproject.toml" ]]; then
    log_error "Orchestra repo appears incomplete (no pyproject.toml)"
    return 1
  fi

  if [[ ! -f "$repo_path/alembic.ini" ]]; then
    log_error "Orchestra repo missing alembic.ini"
    return 1
  fi

  log_success "Orchestra repo found at: $repo_path"
  return 0
}

check_uv() {
  if ! command -v uv &>/dev/null; then
    log_error "uv is not installed (required for orchestra)"
    return 1
  fi
  log_success "uv is available"
  return 0
}

# Get an executable from the in-project .venv, with fallback to uv run.
# The --project flag pins uv to orchestra's environment regardless of the
# caller's cwd (e.g., unity calling orchestra's local.sh), and uv run
# creates the .venv on first use.
#
# Usage: get_venv_executable <repo_path> <executable_name>
# Example: get_venv_executable "/path/to/orchestra" "python"
# Returns: Full path to executable, or "uv run --project <repo> <executable>" as fallback
get_venv_executable() {
  local repo_path="$1"
  local executable="$2"
  local venv_bin="$repo_path/.venv/bin"

  if [[ -x "$venv_bin/$executable" ]]; then
    echo "$venv_bin/$executable"
  else
    echo "uv run --project $repo_path $executable"
  fi
}

seed_billing_defaults() {
  local db_container="$1"

  docker exec "$db_container" psql -q -U orchestra -d orchestra -c "
DO \$\$
BEGIN
  INSERT INTO billing_plan_template (
      id, name, display_name, description,
      billing_mode,
      commit_amount, currency, commit_period, commit_schedule,
      collection_method,
      proration_policy, credits_rollover_policy,
      fx_policy, fx_locked_rate,
      is_custom, is_active, created_at
  ) VALUES (
      1, 'default', 'Default',
      'Platform-default pay-as-you-go plan. Credit-based wallet with auto-recharge support.',
      'CREDITS',
      NULL, 'USD', NULL, NULL,
      'AUTO_CARD',
      'PRORATE', NULL,
      NULL, NULL,
      false, true, now()
  )
  ON CONFLICT (id) DO NOTHING;

  PERFORM setval(
    'billing_plan_template_id_seq',
    GREATEST((SELECT COALESCE(MAX(id), 1) FROM billing_plan_template), 1)
  );

  INSERT INTO plan_group (id, name, display_name, description, is_active)
  VALUES (1, 'default', 'Default', 'Default plan group for local dev / test users', true)
  ON CONFLICT (id) DO NOTHING;

  PERFORM setval('plan_group_id_seq', GREATEST((SELECT MAX(id) FROM plan_group), 1));

  INSERT INTO plan_group_member (group_id, template_id, position, added_at)
  VALUES (1, 1, 0, now())
  ON CONFLICT (group_id, template_id) DO NOTHING;

  UPDATE billing_account
  SET plan_group_id = 1
  WHERE plan_group_id IS NULL;

  INSERT INTO billing_plan_assignment (billing_account_id, template_id, change_reason)
  SELECT ba.id, 1, 'local bootstrap default plan'
  FROM billing_account ba
  WHERE NOT EXISTS (
    SELECT 1
    FROM billing_plan_assignment active_assignment
    WHERE active_assignment.billing_account_id = ba.id
      AND active_assignment.ended_at IS NULL
  );

  WITH active AS (
    SELECT DISTINCT ON (billing_account_id) id, billing_account_id
    FROM billing_plan_assignment
    WHERE ended_at IS NULL
    ORDER BY billing_account_id, started_at DESC, id DESC
  )
  UPDATE billing_account ba
  SET plan_assignment_id = active.id
  FROM active
  WHERE ba.id = active.billing_account_id
    AND ba.plan_assignment_id IS NULL;
END
\$\$;
" 2>&1
}

# Seed the static RBAC system roles and permissions (Owner/Admin/Member/Viewer
# and the project/org/billing/assistant permission set). These are platform
# data, not test fixtures: organization creation (POST /organizations) and all
# org-scoped authorization look up the "Owner" system role, so without these
# rows any org/team flow fails with "Owner system role not found". Migrations
# define the tables but do not populate them, so a fresh local DB needs this
# seed. Mirrors the RBAC section of orchestra/tests/seeding.sql. Idempotent:
# the whole block is skipped once the system roles exist.
seed_system_roles() {
  local db_container="$1"

  docker exec "$db_container" psql -q -U orchestra -d orchestra -c "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM role WHERE is_system_role = true) THEN
    INSERT INTO permission (name, description, resource_type, action)
    SELECT v.name, v.description, v.resource_type, v.action
    FROM (VALUES
      ('project:read', 'View project details', 'project', 'read'),
      ('project:write', 'Edit project', 'project', 'write'),
      ('project:delete', 'Delete project', 'project', 'delete'),
      ('org:read', 'View organization details', 'organization', 'read'),
      ('org:write', 'Edit organization settings, billing, and members', 'organization', 'write'),
      ('org:delete', 'Delete organization', 'organization', 'delete'),
      ('billing:read', 'View billing information, credits, and invoices', 'billing', 'read'),
      ('billing:write', 'Update billing settings, autorecharge, and business profile', 'billing', 'write'),
      ('assistant:read', 'View assistant details', 'assistant', 'read'),
      ('assistant:write', 'Create and edit assistants', 'assistant', 'write'),
      ('assistant:delete', 'Delete assistants', 'assistant', 'delete')
    ) AS v(name, description, resource_type, action)
    WHERE NOT EXISTS (SELECT 1 FROM permission p WHERE p.name = v.name);

    INSERT INTO role (name, description, organization_id, is_system_role) VALUES
      ('Owner', 'Full access to projects and organization', NULL, true),
      ('Admin', 'Full access except deleting organization', NULL, true),
      ('Member', 'Read and write projects, view organization details', NULL, true),
      ('Viewer', 'Read-only access to projects and organization', NULL, true);

    -- Owner: every permission.
    INSERT INTO role_permission (role_id, permission_id)
    SELECT (SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true), id
    FROM permission;

    -- Admin: everything except deleting the organization.
    INSERT INTO role_permission (role_id, permission_id)
    SELECT (SELECT id FROM role WHERE name = 'Admin' AND is_system_role = true), id
    FROM permission WHERE name != 'org:delete';

    -- Member: project read/write, org read, assistant read/write, billing read.
    INSERT INTO role_permission (role_id, permission_id)
    SELECT (SELECT id FROM role WHERE name = 'Member' AND is_system_role = true), id
    FROM permission
    WHERE (resource_type = 'project' AND action IN ('read', 'write'))
       OR (resource_type = 'organization' AND action = 'read')
       OR (resource_type = 'assistant' AND action IN ('read', 'write'))
       OR name = 'billing:read';

    -- Viewer: read-only across resources.
    INSERT INTO role_permission (role_id, permission_id)
    SELECT (SELECT id FROM role WHERE name = 'Viewer' AND is_system_role = true), id
    FROM permission WHERE action = 'read';
  END IF;
END
\$\$;
" 2>&1
}

# =============================================================================
# PostgreSQL Container Management
# =============================================================================

is_db_container_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${ORCHESTRA_DB_CONTAINER}$"
}

is_db_container_exists() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${ORCHESTRA_DB_CONTAINER}$"
}

is_compatible_db_running() {
  local container
  container=$(docker ps --filter "publish=${ORCHESTRA_DB_PORT}" --format "{{.Names}}" 2>/dev/null | head -1)

  if [[ -n "$container" ]]; then
    if docker exec "$container" pg_isready -U orchestra &>/dev/null \
      && db_container_has_orchestra_database "$container"; then
      log_success "Found compatible PostgreSQL container: $container"
      ORCHESTRA_DB_CONTAINER="$container"
      return 0
    fi
  fi
  return 1
}

remove_db_container() {
  # Remove the container regardless of state (running, stopped, or other)
  # Returns 0 if container doesn't exist or was successfully removed
  if ! is_db_container_exists; then
    return 0
  fi

  # Stop if running
  if is_db_container_running; then
    docker stop "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1 || true
  fi

  # Remove the container (force remove handles edge cases like "removing" state)
  if ! docker rm -f "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1; then
    log_error "Failed to remove container '$ORCHESTRA_DB_CONTAINER'"
    return 1
  fi

  return 0
}

db_container_has_orchestra_database() {
  local container="${1:-$ORCHESTRA_DB_CONTAINER}"
  local result=""
  result="$(docker exec "$container" \
    psql -U orchestra -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'orchestra'" \
    2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$result" == "1" ]]
}

ensure_orchestra_database_present() {
  if db_container_has_orchestra_database "$ORCHESTRA_DB_CONTAINER"; then
    return 0
  fi

  log_error "PostgreSQL is running, but database 'orchestra' is missing"
  log_info "Run a destructive local reset: $0 purge && $0 start"
  return 1
}

_ensure_db_container_port_matches() {
  local mapped_port
  mapped_port="$(_db_container_host_port "$ORCHESTRA_DB_CONTAINER")"
  if [[ -n "$mapped_port" && "$mapped_port" == "$ORCHESTRA_DB_PORT" ]]; then
    return 0
  fi
  log_error "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' is mapped to port ${mapped_port:-unknown}, but ORCHESTRA_DB_PORT=$ORCHESTRA_DB_PORT"
  log_info "Re-run with ORCHESTRA_DB_PORT=${mapped_port:-5432}, or stop the container and re-run setup"
  return 1
}

start_db_container() {
  log_info "Starting PostgreSQL container with pgvector..."

  if is_db_container_running; then
    local mapped_port
    mapped_port="$(_db_container_host_port "$ORCHESTRA_DB_CONTAINER")"
    if [[ -n "$mapped_port" && "$mapped_port" == "$ORCHESTRA_DB_PORT" ]]; then
      if ! ensure_orchestra_database_present; then
        return 1
      fi
      log_success "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' already running on port $ORCHESTRA_DB_PORT"
      return 0
    fi
    log_error "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' is running on port ${mapped_port:-unknown}, but ORCHESTRA_DB_PORT=$ORCHESTRA_DB_PORT"
    log_info "Re-run with ORCHESTRA_DB_PORT=${mapped_port:-5432}, or stop the container and re-run setup"
    return 1
  fi

  if is_compatible_db_running; then
    log_success "Using compatible PostgreSQL on port $ORCHESTRA_DB_PORT"
    return 0
  fi

  # If a stopped container already exists, restart it rather than re-creating.
  # This preserves anonymous volumes from pre-named-volume installs and avoids
  # unnecessary churn on the install-and-live path.
  if is_db_container_exists; then
    log_info "Reusing existing container '$ORCHESTRA_DB_CONTAINER' (data preserved)..."
    if ! docker start "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1; then
      log_error "Failed to start existing container '$ORCHESTRA_DB_CONTAINER'"
      log_info "If the container is in a bad state, run: $0 purge && $0 start"
      return 1
    fi
    if ! _ensure_db_container_port_matches; then
      return 1
    fi
  else
    # Fresh start: create the container with a named volume + restart policy
    # so it survives reboots and `unity stop` / `unity restart` cycles.

    # Check if port is already in use by something else
    if lsof -i ":${ORCHESTRA_DB_PORT}" -sTCP:LISTEN &>/dev/null; then
      log_error "Port $ORCHESTRA_DB_PORT is already in use by another process"
      log_info "Stop the conflicting service or use ORCHESTRA_DB_PORT=5433"
      return 1
    fi

    # Ensure the named volume exists. `docker volume create` is idempotent.
    if ! docker volume create "$ORCHESTRA_DB_VOLUME" >/dev/null 2>&1; then
      log_error "Failed to create Docker volume '$ORCHESTRA_DB_VOLUME'"
      return 1
    fi

    # Calculate max_connections based on CPU cores
    local num_cores
    if [[ "$(uname)" == "Darwin" ]]; then
      num_cores=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
    else
      num_cores=$(nproc 2>/dev/null || echo 4)
    fi
    local max_connections=$((num_cores * 100))
    log_info "Setting PostgreSQL max_connections=$max_connections (${num_cores} cores × 100)"

    local pg_flags=(
      "-c" "max_connections=$max_connections"
      "-c" "statement_timeout=120s"
      "-c" "deadlock_timeout=1s"
    )

    # --restart unless-stopped  → container auto-starts when Docker comes back
    #                              (e.g. after a reboot), but stays stopped if
    #                              the user explicitly stopped it.
    # -v <volume>:/var/lib/postgresql/data  → data lives in a named volume,
    #                              so it survives container removal and is
    #                              re-attached on the next `docker run`.
    if ! docker run -d \
      --name "$ORCHESTRA_DB_CONTAINER" \
      --restart unless-stopped \
      -v "${ORCHESTRA_DB_VOLUME}:/var/lib/postgresql/data" \
      -p "${ORCHESTRA_DB_PORT}:5432" \
      -e POSTGRES_PASSWORD=orchestra \
      -e POSTGRES_USER=orchestra \
      -e POSTGRES_DB=orchestra \
      pgvector/pgvector:pg15 \
      postgres "${pg_flags[@]}" >/dev/null; then
      log_error "Failed to start PostgreSQL container"
      return 1
    fi
  fi

  log_info "Waiting for PostgreSQL to be ready..."

  # pg_isready can succeed before initdb finishes creating POSTGRES_DB=orchestra
  # (especially on a cold image pull). Keep waiting for the database itself; only
  # hard-fail once the wait budget is exhausted (or for already-running containers
  # above via ensure_orchestra_database_present).
  local max_attempts=30
  local attempt=0
  while (( attempt < max_attempts )); do
    if docker exec "$ORCHESTRA_DB_CONTAINER" pg_isready -U orchestra &>/dev/null \
      && db_container_has_orchestra_database "$ORCHESTRA_DB_CONTAINER"; then
      log_success "PostgreSQL is ready"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  if docker exec "$ORCHESTRA_DB_CONTAINER" pg_isready -U orchestra &>/dev/null; then
    ensure_orchestra_database_present || return 1
  fi
  log_error "PostgreSQL failed to start within 30 seconds"
  return 1
}

stop_db_container() {
  # Preserve the container (and its named volume) so the next `start` is a
  # cheap restart and data survives. Use `purge_db_container` for the
  # destructive path.
  if ! is_db_container_exists; then
    log_info "No PostgreSQL container to stop (container: $ORCHESTRA_DB_CONTAINER)"
    return 0
  fi
  if ! is_db_container_running; then
    log_info "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' already stopped"
    return 0
  fi

  log_info "Stopping PostgreSQL container '$ORCHESTRA_DB_CONTAINER' (data preserved)..."
  if docker stop "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1; then
    log_success "PostgreSQL container stopped"
  else
    log_error "Failed to stop container '$ORCHESTRA_DB_CONTAINER'"
    return 1
  fi
}

# Destructive: stop, remove the container, and delete the named volume so all
# data is wiped. Used by tests for inter-run isolation and by users who want
# to start from a clean slate.
purge_db_container() {
  if is_db_container_exists; then
    log_info "Removing PostgreSQL container '$ORCHESTRA_DB_CONTAINER'..."
    if ! remove_db_container; then
      log_error "Failed to remove container; aborting purge"
      return 1
    fi
  fi

  # Remove the named volume if it exists. Idempotent.
  if docker volume inspect "$ORCHESTRA_DB_VOLUME" >/dev/null 2>&1; then
    log_info "Removing PostgreSQL data volume '$ORCHESTRA_DB_VOLUME'..."
    if ! docker volume rm "$ORCHESTRA_DB_VOLUME" >/dev/null 2>&1; then
      log_warn "Could not remove volume '$ORCHESTRA_DB_VOLUME' (in use?). Data may be retained."
    else
      log_success "Data volume removed"
    fi
  fi
}

# =============================================================================
# Database Migrations and Seeding
# =============================================================================

export_db_env() {
  export ORCHESTRA_DB_HOST=localhost
  export ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT"
  export ORCHESTRA_DB_USER=orchestra
  export ORCHESTRA_DB_PASS=orchestra
  export ORCHESTRA_DB_BASE=orchestra
}

# Print the database's revision and the checkout's head, space separated, so a
# caller can tell whether the schema matches the code without upgrading.
schema_revisions() {
  local repo_path="$1"

  cd "$repo_path"
  export_db_env

  local alembic_cmd
  alembic_cmd=$(get_venv_executable "$repo_path" "alembic")

  local current head
  current=$($alembic_cmd current 2>/dev/null | grep -oE "^[a-z0-9_]+" | tail -1)
  head=$($alembic_cmd heads 2>/dev/null | grep -oE "^[a-z0-9_]+" | tail -1)
  echo "$current $head"
}

run_migrations() {
  local repo_path="$1"

  log_info "Running database migrations..."

  cd "$repo_path"
  export_db_env

  local alembic_cmd
  alembic_cmd=$(get_venv_executable "$repo_path" "alembic")

  if $alembic_cmd upgrade head 2>&1; then
    log_success "Migrations completed"
    return 0
  else
    log_error "Migrations failed"
    return 1
  fi
}

# Create the platform-owned system projects (Builtins, AssistantJobs).
#
# Migrations create the schema; they do not create these rows. Only the
# self-host bootstrap does, and nothing on the local.sh path used to call it —
# so a database that had been reset came back up without Builtins, and the
# runtime's first catalogue seed failed against a project that did not exist.
# The bootstrap repairs as well as creates, so running it every start is a
# cheap no-op once the rows are there.
bootstrap_platform_projects() {
  local repo_path="$1"
  local bootstrap="$repo_path/scripts/bootstrap_self_host.sh"

  if [[ ! -x "$bootstrap" ]]; then
    log_warn "Missing $bootstrap; skipping platform project bootstrap"
    return 0
  fi

  log_info "Bootstrapping platform projects..."
  # Pass the resolved port through: the bootstrap detects it too, but this run
  # already knows which container it started.
  if ORCHESTRA_REPO_PATH="$repo_path" \
     ORCHESTRA_DB_HOST=localhost \
     ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT" \
     ORCHESTRA_DB_CONTAINER="$ORCHESTRA_DB_CONTAINER" \
     bash "$bootstrap" >/dev/null 2>&1; then
    log_success "Platform projects ready"
    return 0
  fi

  log_warn "Platform project bootstrap failed (Builtins catalogue may not seed)"
  return 1
}

ensure_test_user_coordinator() {
  local test_user_id="$1"

  log_info "Ensuring test user Coordinator..."

  cd "$ORCHESTRA_REPO_PATH"

  export ORCHESTRA_DB_HOST=localhost
  export ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT"
  export ORCHESTRA_DB_USER=orchestra
  export ORCHESTRA_DB_PASS=orchestra
  export ORCHESTRA_DB_BASE=orchestra

  local python_cmd
  python_cmd=$(get_venv_executable "$ORCHESTRA_REPO_PATH" "python")

  if SELF_HOST=1 $python_cmd scripts/ensure_test_user_coordinator.py --user-id "$test_user_id" 2>&1; then
    log_success "Test user Coordinator ready"
    return 0
  fi

  log_error "Failed to provision test user Coordinator"
  return 1
}

ensure_test_user_billing() {
  local db_container="$1"
  local test_user_id="$2"

  docker exec "$db_container" psql -q -U orchestra -d orchestra -c "
DO \$\$
DECLARE
  _user_id text := '$test_user_id';
  _ba_id integer;
  _default_template_id bigint;
  _assignment_id bigint;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM \"user\" WHERE id = _user_id) THEN
    RETURN;
  END IF;

  SELECT billing_account_id INTO _ba_id FROM \"user\" WHERE id = _user_id;

  IF _ba_id IS NULL THEN
    INSERT INTO billing_account (credits, account_status)
    VALUES (10000, 'ACTIVE')
    RETURNING id INTO _ba_id;

    UPDATE \"user\" SET billing_account_id = _ba_id WHERE id = _user_id;
  END IF;

  SELECT id INTO _default_template_id
  FROM billing_plan_template
  WHERE name = 'default' AND is_active = true
  ORDER BY id
  LIMIT 1;

  IF _default_template_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM billing_plan_assignment
    WHERE billing_account_id = _ba_id
      AND ended_at IS NULL
  ) THEN
    INSERT INTO billing_plan_assignment (billing_account_id, template_id, change_reason)
    VALUES (_ba_id, _default_template_id, 'repair missing billing assignment')
    RETURNING id INTO _assignment_id;

    UPDATE billing_account
    SET plan_assignment_id = _assignment_id
    WHERE id = _ba_id;
  END IF;
END
\$\$;
" 2>&1
}

ensure_test_user_api_key() {
  local db_container="$1"
  local test_user_id="$2"
  local test_api_key="$3"

  docker exec "$db_container" psql -q -U orchestra -d orchestra -c "
INSERT INTO api_key (user_id, key)
VALUES ('$test_user_id', '$test_api_key')
ON CONFLICT (key) DO UPDATE SET user_id = EXCLUDED.user_id;
" 2>&1
}

seed_test_user() {
  local test_user_id="${ORCHESTRA_TEST_USER_ID:-test-user-001}"
  local test_api_key="${UNIFY_KEY:-local-test-api-key}"
  local test_email="${ORCHESTRA_TEST_EMAIL:-test@debug.local}"

  log_info "Checking if test user exists..."

  local db_container
  db_container=$(docker ps --filter "publish=${ORCHESTRA_DB_PORT}" --format "{{.Names}}" 2>/dev/null | head -1)

  if [[ -z "$db_container" ]]; then
    log_error "No PostgreSQL container found"
    return 1
  fi

  if ! seed_billing_defaults "$db_container"; then
    log_error "Failed to seed billing defaults"
    return 1
  fi

  if ! seed_system_roles "$db_container"; then
    log_error "Failed to seed system roles"
    return 1
  fi

  local user_exists
  user_exists=$(docker exec "$db_container" psql -U orchestra -d orchestra -tAc \
    "SELECT 1 FROM \"user\" WHERE id = '$test_user_id'" 2>/dev/null || echo "")

  if [[ "$user_exists" == "1" ]]; then
    log_success "Test user already exists"
    ensure_test_user_billing "$db_container" "$test_user_id"
    ensure_test_user_api_key "$db_container" "$test_user_id" "$test_api_key"
    ensure_test_user_coordinator "$test_user_id"
    return $?
  fi

  log_info "Creating test user..."

  # Note: Billing fields (credits, stripe_customer_id, etc.) now live on
  # the billing_account table. The 'user' table only holds
  # profile/identity fields plus a billing_account_id FK.
  docker exec "$db_container" psql -U orchestra -d orchestra -c "
DO \$\$
DECLARE
  _ba_id integer;
  _default_template_id bigint;
  _assignment_id bigint;
BEGIN
  -- Seed the default plan_group that billing_account.plan_group_id points
  -- at by default (bigint DEFAULT 1 NOT NULL + FK). Production DBs have
  -- this row pre-existing as part of platform bootstrap; a fresh local DB
  -- doesn't, and the FK fires when we INSERT into billing_account below.
  -- Idempotent: ON CONFLICT skips if a previous run already seeded it.
  INSERT INTO plan_group (id, name, display_name, description, is_active)
  VALUES (1, 'default', 'Default', 'Default plan group for local dev / test users', true)
  ON CONFLICT (id) DO NOTHING;

  -- Advance the sequence past the seeded id so future plan_group inserts
  -- (e.g. from tests creating their own groups) don't collide on id=1.
  PERFORM setval('plan_group_id_seq', GREATEST((SELECT MAX(id) FROM plan_group), 1));

  -- Only seed if user doesn't already exist
  IF NOT EXISTS (SELECT 1 FROM \"user\" WHERE id = '$test_user_id') THEN
    -- Create a billing_account for the test user
    INSERT INTO billing_account (credits, account_status)
    VALUES (10000, 'ACTIVE')
    RETURNING id INTO _ba_id;

    -- Establish the v2 invariant: every account has an active default plan
    -- assignment (signup normally does this via BillingAccountDAO.create →
    -- assign_default_at_signup). This raw seed must mirror it, otherwise the
    -- account has no 'current' plan and GET /billing/available-plans returns
    -- an empty list — the self-serve plan picker then renders nothing.
    SELECT id INTO _default_template_id
    FROM billing_plan_template
    WHERE name = 'default' AND is_active = true
    ORDER BY id
    LIMIT 1;

    IF _default_template_id IS NULL THEN
      RAISE EXCEPTION 'Missing default billing_plan_template while seeding test user (run migrations first)';
    END IF;

    INSERT INTO billing_plan_assignment (billing_account_id, template_id, change_reason)
    VALUES (_ba_id, _default_template_id, 'seed test user bootstrap')
    RETURNING id INTO _assignment_id;

    UPDATE billing_account SET plan_assignment_id = _assignment_id WHERE id = _ba_id;

    -- Create user record linked to the billing_account
    INSERT INTO \"user\" (id, email, billing_account_id, store_prompts)
    VALUES ('$test_user_id', '$test_email', _ba_id, true);

    -- Create API key
    INSERT INTO api_key (user_id, key)
    VALUES ('$test_user_id', '$test_api_key')
    ON CONFLICT (key) DO NOTHING;

    -- Seed the default "_" project. The unify Python client's
    -- _get_project(required=True) falls back to "_" when no project is
    -- active (see unify/utils/helpers.py), so any test or script that
    -- doesn't explicitly activate a project hits POST /v0/project/_/*.
    -- Production users get this row implicitly when they first use the
    -- SDK; local/test DBs need it seeded.
    INSERT INTO project (user_id, name)
    VALUES ('$test_user_id', '_')
    ON CONFLICT (user_id, name) DO NOTHING;
  END IF;
END
\$\$;
" 2>&1

  if [[ $? -eq 0 ]]; then
    if ! seed_billing_defaults "$db_container"; then
      log_error "Failed to seed billing defaults"
      return 1
    fi
    if ! ensure_test_user_coordinator "$test_user_id"; then
      return 1
    fi
    log_success "Test user created"
    log_info "Test API key: $test_api_key"
    return 0
  else
    log_error "Failed to create test user"
    return 1
  fi
}

get_test_api_key() {
  echo "${UNIFY_KEY:-local-test-api-key}"
}

# =============================================================================
# Orchestra Server Management
# =============================================================================

is_orchestra_server_running() {
  if [[ -f "$ORCHESTRA_SERVER_PIDFILE" ]]; then
    local pid
    pid=$(cat "$ORCHESTRA_SERVER_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

wait_for_server() {
  local max_attempts=60
  local attempt=0

  log_info "Waiting for Orchestra server to be ready..."

  while (( attempt < max_attempts )); do
    if curl -s --connect-timeout 5 --max-time 10 "http://127.0.0.1:${ORCHESTRA_PORT}/v0" &>/dev/null || \
       curl -s --connect-timeout 5 --max-time 10 "http://127.0.0.1:${ORCHESTRA_PORT}/docs" &>/dev/null; then
      log_success "Orchestra server is ready at $LOCAL_ORCHESTRA_URL"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "Orchestra server failed to start within 60 seconds"
  return 1
}

start_orchestra_server() {
  local repo_path="$1"

  log_info "Starting Orchestra FastAPI server..."

  if is_orchestra_server_running; then
    if wait_for_server; then
      log_success "Orchestra server already running"
      return 0
    elif lsof -i ":${ORCHESTRA_PORT}" -sTCP:LISTEN &>/dev/null \
        && [[ "${ORCHESTRA_FORCE_RESTART:-0}" != "1" ]]; then
      # Under load (parallel pytest session init, builtins seeding) health probes
      # can time out while the server is still healthy. Do not SIGKILL a live
      # listener unless the caller explicitly opts into ORCHESTRA_FORCE_RESTART=1.
      log_warn "Orchestra server slow to respond but still listening on port ${ORCHESTRA_PORT}; leaving it running"
      return 0
    else
      log_warn "Server process exists but not responsive, restarting..."
      stop_orchestra_server
    fi
  fi

  if lsof -i ":${ORCHESTRA_PORT}" -sTCP:LISTEN &>/dev/null; then
    if wait_for_server; then
      log_success "Orchestra server already running on port $ORCHESTRA_PORT"
      return 0
    else
      log_error "Port $ORCHESTRA_PORT is in use by another process"
      return 1
    fi
  fi

  cd "$repo_path"

  # Local Orchestra is a dev/test harness only. Self-host billing semantics
  # (charges_billing=false) keep /v0/credits/deduct as a no-op so UniLLM
  # metering never fails auth against the seeded test user.
  export SELF_HOST=1

  # Set environment variables
  # Self-host desktop containers reach Orchestra via host.docker.internal.
  if [[ "${SELF_HOST:-0}" == "1" ]]; then
    export ORCHESTRA_HOST=0.0.0.0
  else
    export ORCHESTRA_HOST=127.0.0.1
  fi
  export ORCHESTRA_PORT="$ORCHESTRA_PORT"
  export ORCHESTRA_DB_HOST=localhost
  export ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT"
  export ORCHESTRA_DB_USER=orchestra
  export ORCHESTRA_DB_PASS=orchestra
  export ORCHESTRA_DB_BASE=orchestra
  export ORCHESTRA_RELOAD=false
  export ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS="$ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS"

  # Admin key must stay distinct from the seeded user UNIFY_KEY. When they match,
  # auth_api_key treats the bearer as __system__ and personal projects (UnityTests)
  # become invisible to local Unity tests.
  local test_api_key="${UNIFY_KEY:-local-test-api-key}"
  if [[ -z "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
    export ORCHESTRA_ADMIN_KEY="local-admin-key"
  fi
  if [[ "${ORCHESTRA_ADMIN_KEY}" == "$test_api_key" ]]; then
    log_error "ORCHESTRA_ADMIN_KEY must differ from UNIFY_KEY (both are '${ORCHESTRA_ADMIN_KEY}'). Set ORCHESTRA_ADMIN_KEY=local-admin-key and restart Orchestra."
    return 1
  fi
  export ORCHESTRA_ADMIN_KEY

  # API keys for embedding and LLM operations
  # Orchestra Python code uses get_env() which checks ORCHESTRA_* prefix first,
  # then falls back to standard provider names.
  [[ -n "${OPENROUTER_API_KEY:-}" ]] && export OPENROUTER_API_KEY
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && export ANTHROPIC_API_KEY
  [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] && export GOOGLE_APPLICATION_CREDENTIALS

  # Comms gateway URL so /v0/features can probe channel availability (phone,
  # whatsapp, discord) and Console can surface those channels. In self-host the
  # bundled unity.gateway serves comms locally; default to it when unset.
  if [[ -z "${UNITY_COMMS_URL:-}" && "${SELF_HOST:-0}" == "1" ]]; then
    UNITY_COMMS_URL="http://127.0.0.1:${UNIFY_GATEWAY_PORT:-8001}"
  fi
  [[ -n "${UNITY_COMMS_URL:-}" ]] && export UNITY_COMMS_URL
  [[ -n "${UNITY_ADAPTERS_URL:-}" ]] && export UNITY_ADAPTERS_URL
  [[ -n "${COMMUNICATION_URL:-}" ]] && export COMMUNICATION_URL
  [[ -n "${COMMS_URL:-}" ]] && export COMMS_URL

  if [[ "${SELF_HOST:-0}" == "1" ]]; then
    [[ -n "${COMPOSIO_API_KEY:-}" ]] && export COMPOSIO_API_KEY
    [[ -n "${COMPOSIO_WEBHOOK_SECRET:-}" ]] && export COMPOSIO_WEBHOOK_SECRET
    [[ -n "${ORCHESTRA_TRIGGER_CALLBACK_BASE_URL:-}" ]] && export ORCHESTRA_TRIGGER_CALLBACK_BASE_URL
    if [[ -z "${TRIGGER_EVENT_WRAPPING_MASTER_KEY:-}" ]]; then
      export TRIGGER_EVENT_WRAPPING_MASTER_KEY="test-master-key-material"
    else
      export TRIGGER_EVENT_WRAPPING_MASTER_KEY
    fi
    if [[ -n "${TRIGGER_EVENT_PRIVATE_ROOT:-}" ]]; then
      export TRIGGER_EVENT_PRIVATE_ROOT
      mkdir -p "$TRIGGER_EVENT_PRIVATE_ROOT"
    fi
  fi

  # Optional logging directories
  if [[ -n "${ORCHESTRA_LOG_DIR:-}" ]]; then
    mkdir -p "$ORCHESTRA_LOG_DIR"
    export ORCHESTRA_LOG_DIR
    log_info "Logging enabled at: $ORCHESTRA_LOG_DIR/"
  fi

  if [[ -n "${ORCHESTRA_OTEL_LOG_DIR:-}" ]]; then
    mkdir -p "$ORCHESTRA_OTEL_LOG_DIR"
    export ORCHESTRA_OTEL_LOG_DIR
  fi

  # Calculate workers
  local num_cores
  if [[ "$(uname)" == "Darwin" ]]; then
    num_cores=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
  else
    num_cores=$(nproc 2>/dev/null || echo 4)
  fi
  # Default 1 worker for local dev/tests. Multi-worker uvicorn multiplies memory
  # and startup time (~14 processes on this machine) without helping single-user runs.
  local workers="${ORCHESTRA_WORKERS:-1}"
  log_info "Starting Orchestra with $workers workers"

  # Get virtualenv python path - prefer the in-project .venv so the right
  # environment is used even when called from another repo's context
  local venv_python
  venv_python=$(get_venv_executable "$ORCHESTRA_REPO_PATH" "python")
  log_info "Using python: $venv_python"

  # Set file descriptor limit
  local fd_limit=$((num_cores * 750))
  if (( fd_limit < 4096 )); then
    fd_limit=4096
  fi
  log_info "Setting file descriptor limit to $fd_limit"

  export ORCHESTRA_WORKERS_COUNT="$workers"
  [[ -n "${SELF_HOST:-}" ]] && export SELF_HOST
  [[ -n "${STAGING:-}" ]] && export STAGING

  # Start server (use setsid if available for proper process isolation).
  # NB: $venv_python is left unquoted on purpose — when no in-project .venv
  # exists, get_venv_executable falls back to the multi-word "uv run
  # --project <repo> python", which must word-split into separate argv
  # entries (same pattern as $alembic_cmd above). Quoting it would exec the
  # whole string as a single, non-existent binary.
  if command -v setsid &>/dev/null; then
    setsid bash -c "ulimit -n $fd_limit; exec $venv_python -m orchestra" > "$ORCHESTRA_SERVER_LOGFILE" 2>&1 &
  else
    bash -c "ulimit -n $fd_limit; exec $venv_python -m orchestra" > "$ORCHESTRA_SERVER_LOGFILE" 2>&1 &
  fi
  local pid=$!
  disown $pid 2>/dev/null || true
  echo "$pid" > "$ORCHESTRA_SERVER_PIDFILE"

  # Write config file for external tools (parallel_run, worktrees) to discover settings.
  {
    echo "ORCHESTRA_LOG_DIR=${ORCHESTRA_LOG_DIR:-}"
    echo "ORCHESTRA_OTEL_LOG_DIR=${ORCHESTRA_OTEL_LOG_DIR:-}"
    echo "ORCHESTRA_DB_PORT=${ORCHESTRA_DB_PORT}"
  } > "$ORCHESTRA_SERVER_CONFIGFILE"

  log_info "Orchestra server started with PID $pid"

  if wait_for_server; then
    return 0
  else
    log_error "Check logs at: $ORCHESTRA_SERVER_LOGFILE"
    return 1
  fi
}

stop_orchestra_server() {
  if [[ -f "$ORCHESTRA_SERVER_PIDFILE" ]]; then
    local pid
    pid=$(cat "$ORCHESTRA_SERVER_PIDFILE")

    if kill -0 "$pid" 2>/dev/null; then
      log_info "Stopping Orchestra server (PID $pid)..."

      local pgid
      pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')

      local my_pgid
      my_pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')

      if [[ -n "$pgid" && "$pgid" != "0" && "$pgid" != "$my_pgid" ]]; then
        kill -- -"$pgid" 2>/dev/null || true
      else
        kill "$pid" 2>/dev/null || true
      fi

      local attempt=0
      while (( attempt < 10 )); do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 1
        ((attempt++)) || true
      done

      if kill -0 "$pid" 2>/dev/null; then
        if [[ -n "$pgid" && "$pgid" != "0" && "$pgid" != "$my_pgid" ]]; then
          kill -9 -- -"$pgid" 2>/dev/null || true
        else
          kill -9 "$pid" 2>/dev/null || true
        fi
      fi
    fi

    rm -f "$ORCHESTRA_SERVER_PIDFILE"
    rm -f "$ORCHESTRA_SERVER_CONFIGFILE"
  fi

  pkill -9 -f -- "-m orchestra" 2>/dev/null || true

  local port_pids
  port_pids=$(lsof -t -i ":${ORCHESTRA_PORT}" 2>/dev/null || true)
  if [[ -n "$port_pids" ]]; then
    log_info "Killing orphaned processes on port $ORCHESTRA_PORT..."
    echo "$port_pids" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi

  # Clear prometheus multiprocess directory
  local prom_dir
  prom_dir="$(python3 -c 'from tempfile import gettempdir; print(gettempdir())' 2>/dev/null)/prom"
  if [[ -d "$prom_dir" ]]; then
    rm -rf "$prom_dir"
    log_info "Cleared prometheus directory: $prom_dir"
  fi

  log_success "Orchestra server stopped"
}

# =============================================================================
# Main Commands
# =============================================================================

cmd_seed() {
  echo "Seeding local Orchestra platform projects + test user + billing defaults..."

  if ! check_docker; then
    return 1
  fi

  if ! start_db_container; then
    return 1
  fi

  # Seeding goes through the ORM, which selects every column the models
  # declare, so it needs the schema the current code expects. A database that
  # already exists is exactly the one that can be behind — it keeps whatever
  # schema it had when it was last migrated while the code moves on — and the
  # seed then dies on a column the models have and the database does not.
  #
  # Who may fix that depends on who owns the database. An isolated database is
  # ours to upgrade, and upgrading is a no-op once it is current. The full
  # stack's database is not: it is being served right now by the code the stack
  # started with, so migrating underneath it can drop a column that running
  # code still selects. There the honest move is to report the drift and let
  # the stack restart, which migrates and reloads the server together.
  if ! check_orchestra_repo "$ORCHESTRA_REPO_PATH"; then
    return 1
  fi

  if refuse_isolated_when_full_stack_active "migrate" >/dev/null 2>&1; then
    if ! run_migrations "$ORCHESTRA_REPO_PATH"; then
      return 1
    fi
  else
    local revisions current head
    revisions=$(schema_revisions "$ORCHESTRA_REPO_PATH")
    current=${revisions% *}
    head=${revisions#* }
    if [[ -n "$head" && "$current" != "$head" ]]; then
      log_error "Database schema is behind this checkout ($current, head is $head)."
      log_info "The running stack is serving that schema, so upgrading it here would break the live server."
      log_info "Restart the stack to migrate and reload together:"
      log_info "  bash unify-deploy/selfhost/stack.sh up --durable"
      return 1
    fi
  fi

  # Ahead of the skip check: the system projects are platform data, not part of
  # the test user, so opting out of the latter must not skip them. This is the
  # repair path for a database that already exists, which is exactly the state
  # a reset leaves behind.
  bootstrap_platform_projects "$ORCHESTRA_REPO_PATH" || true

  if [[ "$ORCHESTRA_SKIP_TEST_USER" == "1" ]]; then
    log_info "Skipping local test user seed (ORCHESTRA_SKIP_TEST_USER=1)"
    return 0
  fi

  seed_test_user
}

cmd_start() {
  refuse_isolated_when_full_stack_active start || return 1

  echo "=============================================="
  echo "Starting Local Orchestra"
  echo "=============================================="
  echo ""

  if ! check_docker; then
    log_warn "Docker not available, falling back to staging URL"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  if ! check_orchestra_repo "$ORCHESTRA_REPO_PATH"; then
    log_warn "Orchestra repo not found, falling back to staging URL"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  if ! check_uv; then
    log_warn "uv not available, falling back to staging URL"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  echo ""

  if ! start_db_container; then
    log_error "Failed to start database"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  if ! run_migrations "$ORCHESTRA_REPO_PATH"; then
    log_error "Failed to run migrations"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  # Not fatal: a server without the system projects still serves every ordinary
  # request, and the failure is worth surfacing rather than blocking a start on.
  bootstrap_platform_projects "$ORCHESTRA_REPO_PATH" || true

  if [[ "$ORCHESTRA_SKIP_TEST_USER" == "1" ]]; then
    log_info "Skipping local test user seed (ORCHESTRA_SKIP_TEST_USER=1)"
  elif ! seed_test_user; then
    log_warn "Failed to seed test user (tests may fail without auth)"
  fi

  if ! start_orchestra_server "$ORCHESTRA_REPO_PATH"; then
    log_error "Failed to start Orchestra server"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
    return 1
  fi

  local test_api_key
  test_api_key=$(get_test_api_key)

  echo ""
  echo "=============================================="
  log_success "Local Orchestra is ready!"
  echo "=============================================="
  echo ""
  echo "To use in your shell:"
  echo "  export UNIFY_BASE_URL='$LOCAL_ORCHESTRA_URL'"
  echo "  export UNIFY_KEY='$test_api_key'"
  echo ""
  echo "Or source this script:"
  echo "  eval \"\$(./local_orchestra.sh)\""
  echo ""

  echo "export UNIFY_BASE_URL='$LOCAL_ORCHESTRA_URL'"
  echo "export UNIFY_KEY='$test_api_key'"

  return 0
}

cmd_stop() {
  echo "Stopping Local Orchestra..."
  echo ""

  stop_orchestra_server
  stop_db_container

  echo ""
  log_success "Local Orchestra stopped (data preserved; \`$0 start\` resumes from here)"
}

cmd_restart() {
  refuse_isolated_when_full_stack_active restart || return 1

  cmd_stop
  echo ""
  cmd_start
}

cmd_purge() {
  refuse_isolated_when_full_stack_active purge || return 1

  echo "Purging Local Orchestra (destroys all local data)..."
  echo ""

  stop_orchestra_server
  purge_db_container

  echo ""
  log_success "Local Orchestra purged. Next \`$0 start\` will create a fresh database."
}

cmd_status() {
  echo "Local Orchestra Status"
  echo "======================"
  echo ""

  echo -n "Docker: "
  if check_docker 2>/dev/null; then
    echo -e "${GREEN}available${NC}"
  else
    echo -e "${RED}not available${NC}"
  fi

  echo -n "PostgreSQL Container: "
  if is_db_container_running; then
    echo -e "${GREEN}running ($ORCHESTRA_DB_CONTAINER)${NC}"
  elif is_compatible_db_running; then
    echo -e "${GREEN}running ($ORCHESTRA_DB_CONTAINER)${NC}"
  else
    echo -e "${RED}not running${NC}"
  fi

  echo -n "Orchestra Server: "
  if is_orchestra_server_running; then
    if wait_for_server 2>/dev/null; then
      echo -e "${GREEN}running and responsive${NC}"
    else
      echo -e "${YELLOW}running but not responsive${NC}"
    fi
  else
    echo -e "${RED}not running${NC}"
  fi

  echo ""
  echo "Configuration:"
  echo "  Orchestra Repo: $ORCHESTRA_REPO_PATH"
  echo "  Prefix:         $ORCHESTRA_PREFIX"
  echo "  FastAPI Port:   $ORCHESTRA_PORT"
  echo "  Database Port:  $ORCHESTRA_DB_PORT"
  echo "  Local URL:      $LOCAL_ORCHESTRA_URL"
  echo ""
}

cmd_check() {
  if curl -s --connect-timeout 5 --max-time 10 "http://127.0.0.1:${ORCHESTRA_PORT}/v0" &>/dev/null || \
     curl -s --connect-timeout 5 --max-time 10 "http://127.0.0.1:${ORCHESTRA_PORT}/docs" &>/dev/null; then
    echo "$LOCAL_ORCHESTRA_URL"
    return 0
  fi
  return 1
}

cmd_env() {
  local test_api_key
  test_api_key=$(get_test_api_key)

  if cmd_check &>/dev/null; then
    echo "export UNIFY_BASE_URL='$LOCAL_ORCHESTRA_URL'"
    echo "export UNIFY_KEY='$test_api_key'"
  else
    echo "# Local orchestra not running, using staging"
    echo "export UNIFY_BASE_URL='$STAGING_URL'"
  fi
}

# =============================================================================
# Entry Point
# =============================================================================

main() {
  local cmd=""

  while (( "$#" )); do
    case "$1" in
      -h|--help)
        cmd="help"
        shift
        ;;
      -*)
        case "$1" in
          --stop) cmd="stop"; shift ;;
          --restart) cmd="restart"; shift ;;
          --purge) cmd="purge"; shift ;;
          --status) cmd="status"; shift ;;
          --check) cmd="check"; shift ;;
          --env) cmd="env"; shift ;;
          --skip-test-user) ORCHESTRA_SKIP_TEST_USER=1; shift ;;
          *)
            log_error "Unknown flag: $1"
            echo "Run '$0 --help' for usage"
            exit 1
            ;;
        esac
        ;;
      *)
        if [[ -z "$cmd" ]]; then
          cmd="$1"
        fi
        shift
        ;;
    esac
  done

  cmd="${cmd:-start}"

  case "$cmd" in
    start)
      cmd_start
      ;;
    stop)
      cmd_stop
      ;;
    restart)
      cmd_restart
      ;;
    purge)
      cmd_purge
      ;;
    status)
      cmd_status
      ;;
    check)
      cmd_check
      ;;
    seed)
      cmd_seed
      ;;
    env)
      cmd_env
      ;;
    help)
      echo "Usage: $0 [command]"
      echo ""
      echo "Commands:"
      echo "  start    Start local orchestra (default; preserves data)"
      echo "  stop     Stop local orchestra (preserves data)"
      echo "  restart  Stop then start (preserves data)"
      echo "  purge    Destroy container + named data volume (wipes all data)"
      echo "  status   Show status"
      echo "  check    Quick check if running (returns URL or exits 1)"
      echo "  seed     Ensure test user, API key, and billing defaults (idempotent)"
      echo "  env      Output environment variables for shell eval"
      echo ""
      echo "Environment Variables:"
      echo "  ORCHESTRA_REPO_PATH     Path to orchestra repo (default: auto-detect)"
      echo "  ORCHESTRA_PORT          FastAPI port (default: 8000)"
      echo "  ORCHESTRA_DB_PORT       PostgreSQL port (default: 5432)"
      echo "  ORCHESTRA_PREFIX        Prefix for container/PID names (default: 'orchestra')"
      echo "  ORCHESTRA_WORKERS       Number of uvicorn workers (default: CPU cores)"
      echo "  ORCHESTRA_LOG_DIR       Directory for orchestra logs (optional)"
      echo "  ORCHESTRA_OTEL_LOG_DIR  Directory for OpenTelemetry traces (optional)"
      echo "  ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS  Shutdown after inactivity (default: 600)"
      echo ""
      echo "Test User (seeded by default on start/restart):"
      echo "  ORCHESTRA_TEST_USER_ID  Test user ID (default: 'test-user-001')"
      echo "  ORCHESTRA_TEST_EMAIL    Test user email (default: 'test@debug.local')"
      echo "  ORCHESTRA_SKIP_TEST_USER Set to 1 to skip local test-user seeding"
      echo "  UNIFY_KEY               API key for test user (default: 'local-test-api-key')"
      echo "  ORCHESTRA_ADMIN_KEY     Admin bearer (default: 'local-admin-key'; must differ from UNIFY_KEY)"
      echo ""
      echo "Examples:"
      echo "  $0 start                              # Start orchestra"
      echo "  ORCHESTRA_PREFIX=myapp $0 start       # Start with custom prefix"
      echo "  eval \"\$($0 env)\"                     # Set env vars"
      ;;
    *)
      log_error "Unknown command: $cmd"
      echo "Run '$0 --help' for usage"
      exit 1
      ;;
  esac
}

main "$@"
