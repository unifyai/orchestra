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
# Usage:
#   ./local_orchestra.sh start    # Start and wait for ready
#   ./local_orchestra.sh stop     # Stop local orchestra
#   ./local_orchestra.sh restart  # Stop then start (wipes database)
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
# Test user (always seeded for local development):
#   ORCHESTRA_TEST_USER_ID  Test user ID (default: "test-user-001")
#   ORCHESTRA_TEST_EMAIL    Test user email (default: "test@debug.local")
#   UNIFY_KEY               API key for test user (default: "local-test-api-key")
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
ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-5432}"

# Inactivity timeout (seconds) - server shuts down after this period of no requests
ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS="${ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS:-600}"

# Derived names using prefix
ORCHESTRA_DB_CONTAINER="${ORCHESTRA_PREFIX}-local-db"
ORCHESTRA_SERVER_PIDFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.pid"
ORCHESTRA_SERVER_LOGFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.log"
ORCHESTRA_SERVER_CONFIGFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.config"

# URLs
LOCAL_ORCHESTRA_URL="http://127.0.0.1:${ORCHESTRA_PORT}/v0"
STAGING_URL="https://orchestra-staging-lz5fmz6i7q-ew.a.run.app/v0"

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

check_poetry() {
  if ! command -v poetry &>/dev/null; then
    log_error "Poetry is not installed (required for orchestra)"
    return 1
  fi
  log_success "Poetry is available"
  return 0
}

# Get an executable from the in-project .venv, with fallback to poetry run.
# This avoids issues where poetry picks up the wrong virtualenv when called
# from a different repo's context (e.g., unity calling orchestra's local.sh).
#
# Usage: get_venv_executable <repo_path> <executable_name>
# Example: get_venv_executable "/path/to/orchestra" "python"
# Returns: Full path to executable, or "poetry run <executable>" as fallback
get_venv_executable() {
  local repo_path="$1"
  local executable="$2"
  local venv_bin="$repo_path/.venv/bin"

  if [[ -x "$venv_bin/$executable" ]]; then
    echo "$venv_bin/$executable"
  else
    # Fallback to poetry - may work if environment is clean
    echo "poetry run $executable"
  fi
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
    if docker exec "$container" pg_isready -U orchestra -d orchestra &>/dev/null; then
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

start_db_container() {
  log_info "Starting PostgreSQL container with pgvector..."

  if is_db_container_running; then
    log_success "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' already running"
    return 0
  fi

  # Remove any existing container (stopped or in other states)
  if is_db_container_exists; then
    log_info "Removing existing container..."
    if ! remove_db_container; then
      return 1
    fi
  fi

  # Check if port is already in use by something else
  if lsof -i ":${ORCHESTRA_DB_PORT}" -sTCP:LISTEN &>/dev/null; then
    log_error "Port $ORCHESTRA_DB_PORT is already in use by another process"
    log_info "Stop the conflicting service or use ORCHESTRA_DB_PORT=5433"
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

  if ! docker run -d \
    --name "$ORCHESTRA_DB_CONTAINER" \
    -p "${ORCHESTRA_DB_PORT}:5432" \
    -e POSTGRES_PASSWORD=orchestra \
    -e POSTGRES_USER=orchestra \
    -e POSTGRES_DB=orchestra \
    pgvector/pgvector:pg15 \
    postgres "${pg_flags[@]}" >/dev/null; then
    log_error "Failed to start PostgreSQL container"
    return 1
  fi

  log_info "Waiting for PostgreSQL to be ready..."

  local max_attempts=30
  local attempt=0
  while (( attempt < max_attempts )); do
    if docker exec "$ORCHESTRA_DB_CONTAINER" pg_isready -U orchestra &>/dev/null; then
      log_success "PostgreSQL is ready"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "PostgreSQL failed to start within 30 seconds"
  return 1
}

stop_db_container() {
  if is_db_container_exists; then
    local was_running=false
    if is_db_container_running; then
      was_running=true
    fi

    log_info "Removing PostgreSQL container '$ORCHESTRA_DB_CONTAINER'..."
    if remove_db_container; then
      if [[ "$was_running" == "true" ]]; then
        log_success "PostgreSQL container stopped and removed"
      else
        log_success "PostgreSQL container removed (was not running)"
      fi
    fi
  else
    log_info "No PostgreSQL container to stop (container: $ORCHESTRA_DB_CONTAINER)"
  fi
}

# =============================================================================
# Database Migrations and Seeding
# =============================================================================

run_migrations() {
  local repo_path="$1"

  log_info "Running database migrations..."

  cd "$repo_path"

  export ORCHESTRA_DB_HOST=localhost
  export ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT"
  export ORCHESTRA_DB_USER=orchestra
  export ORCHESTRA_DB_PASS=orchestra
  export ORCHESTRA_DB_BASE=orchestra

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

  local user_exists
  user_exists=$(docker exec "$db_container" psql -U orchestra -d orchestra -tAc \
    "SELECT 1 FROM \"user\" WHERE id = '$test_user_id'" 2>/dev/null || echo "")

  if [[ "$user_exists" == "1" ]]; then
    log_success "Test user already exists"
    return 0
  fi

  log_info "Creating test user..."

  # Note: Billing fields (credits, stripe_customer_id, autorecharge, etc.)
  # now live on the billing_account table. The 'user' table only holds
  # profile/identity fields plus a billing_account_id FK.
  docker exec "$db_container" psql -U orchestra -d orchestra -c "
DO \$\$
DECLARE
  _ba_id integer;
BEGIN
  -- Only seed if user doesn't already exist
  IF NOT EXISTS (SELECT 1 FROM \"user\" WHERE id = '$test_user_id') THEN
    -- Create a billing_account for the test user
    INSERT INTO billing_account (credits, autorecharge, autorecharge_threshold, autorecharge_qty, account_status, tier)
    VALUES (10000, false, 0, 25, 'ACTIVE', 'developer')
    RETURNING id INTO _ba_id;

    -- Create user record linked to the billing_account
    INSERT INTO \"user\" (id, email, billing_account_id, store_prompts)
    VALUES ('$test_user_id', '$test_email', _ba_id, true);

    -- Create API key
    INSERT INTO api_key (user_id, key)
    VALUES ('$test_user_id', '$test_api_key')
    ON CONFLICT (key) DO NOTHING;
  END IF;
END
\$\$;
" 2>&1

  if [[ $? -eq 0 ]]; then
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
    else
      log_warn "Server process exists but not responsive, restarting..."
      stop_orchestra_server
    fi
  fi

  if lsof -i ":${ORCHESTRA_PORT}" &>/dev/null; then
    if wait_for_server; then
      log_success "Orchestra server already running on port $ORCHESTRA_PORT"
      return 0
    else
      log_error "Port $ORCHESTRA_PORT is in use by another process"
      return 1
    fi
  fi

  cd "$repo_path"

  # Set environment variables
  export ORCHESTRA_HOST=127.0.0.1
  export ORCHESTRA_PORT="$ORCHESTRA_PORT"
  export ORCHESTRA_DB_HOST=localhost
  export ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT"
  export ORCHESTRA_DB_USER=orchestra
  export ORCHESTRA_DB_PASS=orchestra
  export ORCHESTRA_DB_BASE=orchestra
  export ORCHESTRA_RELOAD=false
  export ORCHESTRA_WORKERS_COUNT=1
  export ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS="$ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS"

  # API keys for embedding and LLM operations
  # Orchestra Python code uses get_env() which checks ORCHESTRA_* prefix first, then
  # falls back to standard names (OPENAI_API_KEY, ANTHROPIC_API_KEY).
  # litellm uses the standard names directly, so we export them if set.
  [[ -n "${OPENAI_API_KEY:-}" ]] && export OPENAI_API_KEY
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && export ANTHROPIC_API_KEY
  [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] && export GOOGLE_APPLICATION_CREDENTIALS

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
  local workers="${ORCHESTRA_WORKERS:-$num_cores}"
  log_info "Starting Orchestra with $workers workers"

  # Get virtualenv python path - use in-project .venv to avoid poetry picking
  # up wrong environment when called from another repo's context
  local venv_python
  venv_python=$(get_venv_executable "$ORCHESTRA_REPO_PATH" "python")
  log_info "Using python: $venv_python"

  # Set file descriptor limit
  local fd_limit=$((num_cores * 750))
  if (( fd_limit < 4096 )); then
    fd_limit=4096
  fi
  log_info "Setting file descriptor limit to $fd_limit"

  # Start server (use setsid if available for proper process isolation)
  if command -v setsid &>/dev/null; then
    setsid bash -c "ulimit -n $fd_limit; exec env ORCHESTRA_WORKERS_COUNT=$workers $venv_python -m orchestra" > "$ORCHESTRA_SERVER_LOGFILE" 2>&1 &
  else
    bash -c "ulimit -n $fd_limit; exec env ORCHESTRA_WORKERS_COUNT=$workers $venv_python -m orchestra" > "$ORCHESTRA_SERVER_LOGFILE" 2>&1 &
  fi
  local pid=$!
  disown $pid 2>/dev/null || true
  echo "$pid" > "$ORCHESTRA_SERVER_PIDFILE"

  # Write config file with logging directories for external tools to check
  {
    echo "ORCHESTRA_LOG_DIR=${ORCHESTRA_LOG_DIR:-}"
    echo "ORCHESTRA_OTEL_LOG_DIR=${ORCHESTRA_OTEL_LOG_DIR:-}"
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

cmd_start() {
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

  if ! check_poetry; then
    log_warn "Poetry not available, falling back to staging URL"
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

  # Always seed test user for local development (required for authentication)
  if ! seed_test_user; then
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
  log_success "Local Orchestra stopped"
}

cmd_restart() {
  cmd_stop
  echo ""
  cmd_start
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
          --status) cmd="status"; shift ;;
          --check) cmd="check"; shift ;;
          --env) cmd="env"; shift ;;
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
    status)
      cmd_status
      ;;
    check)
      cmd_check
      ;;
    env)
      cmd_env
      ;;
    help)
      echo "Usage: $0 [command]"
      echo ""
      echo "Commands:"
      echo "  start    Start local orchestra (default)"
      echo "  stop     Stop local orchestra"
      echo "  restart  Stop then start (wipes database)"
      echo "  status   Show status"
      echo "  check    Quick check if running (returns URL or exits 1)"
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
      echo "Test User (always seeded on start/restart):"
      echo "  ORCHESTRA_TEST_USER_ID  Test user ID (default: 'test-user-001')"
      echo "  ORCHESTRA_TEST_EMAIL    Test user email (default: 'test@debug.local')"
      echo "  UNIFY_KEY               API key for test user (default: 'local-test-api-key')"
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
