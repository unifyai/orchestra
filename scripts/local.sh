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
#
# Seeding (optional):
#   ORCHESTRA_SEED_USER     Set to "1" to seed a test user
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

# Derived names using prefix
ORCHESTRA_DB_CONTAINER="${ORCHESTRA_PREFIX}-local-db"
ORCHESTRA_SERVER_PIDFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.pid"
ORCHESTRA_SERVER_LOGFILE="/tmp/${ORCHESTRA_PREFIX}-local-server.log"

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

check_docker() {
  if ! command -v docker &>/dev/null; then
    log_error "Docker is not installed"
    return 1
  fi

  if ! docker info &>/dev/null; then
    log_error "Docker daemon is not running"
    return 1
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

start_db_container() {
  log_info "Starting PostgreSQL container with pgvector..."

  if is_db_container_running; then
    log_success "PostgreSQL container '$ORCHESTRA_DB_CONTAINER' already running"
    return 0
  fi

  if is_compatible_db_running; then
    return 0
  fi

  if is_db_container_exists; then
    log_info "Removing stopped container..."
    docker rm "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1 || true
  fi

  # Check if port is already in use
  if lsof -i ":${ORCHESTRA_DB_PORT}" -sTCP:LISTEN &>/dev/null; then
    log_warn "Port $ORCHESTRA_DB_PORT is already in use"

    if docker run --rm --network host pgvector/pgvector:pg15 \
         pg_isready -h localhost -p "$ORCHESTRA_DB_PORT" -U orchestra &>/dev/null 2>&1; then
      log_success "PostgreSQL already available on port $ORCHESTRA_DB_PORT"
      return 0
    fi

    if PGPASSWORD=orchestra psql -h localhost -p "$ORCHESTRA_DB_PORT" -U orchestra -d orchestra -c "SELECT 1" &>/dev/null 2>&1; then
      log_success "PostgreSQL with orchestra database available on port $ORCHESTRA_DB_PORT"
      return 0
    fi

    log_error "Port $ORCHESTRA_DB_PORT is in use but not by a compatible PostgreSQL"
    log_info "Try: docker stop <container> or use ORCHESTRA_DB_PORT=5433"
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

  docker run -d \
    --name "$ORCHESTRA_DB_CONTAINER" \
    -p "${ORCHESTRA_DB_PORT}:5432" \
    -e POSTGRES_PASSWORD=orchestra \
    -e POSTGRES_USER=orchestra \
    -e POSTGRES_DB=orchestra \
    pgvector/pgvector:pg15 \
    postgres "${pg_flags[@]}" >/dev/null

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
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${ORCHESTRA_DB_CONTAINER}$"; then
    log_info "Stopping PostgreSQL container '$ORCHESTRA_DB_CONTAINER'..."
    docker stop "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1 || true
    docker rm "$ORCHESTRA_DB_CONTAINER" >/dev/null 2>&1 || true
    log_success "PostgreSQL container stopped"
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

  if poetry run alembic upgrade head 2>&1; then
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
    "SELECT 1 FROM users WHERE id = '$test_user_id'" 2>/dev/null || echo "")

  if [[ "$user_exists" == "1" ]]; then
    log_success "Test user already exists"
    return 0
  fi

  log_info "Creating test user..."

  docker exec "$db_container" psql -U orchestra -d orchestra -c "
-- Create billing user (users table)
INSERT INTO users (id, credits, stripe_customer_id, autorecharge, autorecharge_threshold, autorecharge_qty, store_prompts, frozen)
VALUES ('$test_user_id', 10000, null, false, 0, 0, true, false)
ON CONFLICT (id) DO NOTHING;

-- Create auth user record
INSERT INTO auth_user (id, email)
VALUES ('$test_user_id', '$test_email')
ON CONFLICT (id) DO NOTHING;

-- Set assistant hiring approval to approved
UPDATE auth_user
SET assistant_hiring_approval = 'approved'
WHERE id = '$test_user_id';

-- Create API key
INSERT INTO api_key (user_id, key)
VALUES ('$test_user_id', '$test_api_key')
ON CONFLICT (key) DO NOTHING;
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
    if curl -s "http://127.0.0.1:${ORCHESTRA_PORT}/v0" &>/dev/null || \
       curl -s "http://127.0.0.1:${ORCHESTRA_PORT}/docs" &>/dev/null; then
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

  # GCP credentials for BucketService
  if [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
    export ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON="$GOOGLE_APPLICATION_CREDENTIALS"
  fi

  # API keys for embedding and LLM operations
  [[ -n "${OPENAI_API_KEY:-}" ]] && export OPENAI_API_KEY
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && export ANTHROPIC_API_KEY

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

  # Get virtualenv python path
  local venv_python
  venv_python=$(poetry env info --executable 2>/dev/null)
  if [[ -z "$venv_python" || ! -x "$venv_python" ]]; then
    log_warn "Could not get virtualenv python path, falling back to poetry run"
    venv_python="poetry run python"
  else
    log_info "Using virtualenv python: $venv_python"
  fi

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

  # Optional: seed test user if requested
  if [[ "${ORCHESTRA_SEED_USER:-}" == "1" ]]; then
    if ! seed_test_user; then
      log_warn "Failed to seed test user (tests may fail without auth)"
    fi
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
  if [[ "${ORCHESTRA_SEED_USER:-}" == "1" ]]; then
    echo "  export UNIFY_KEY='$test_api_key'"
  fi
  echo ""
  echo "Or source this script:"
  echo "  eval \"\$(./local_orchestra.sh)\""
  echo ""

  echo "export UNIFY_BASE_URL='$LOCAL_ORCHESTRA_URL'"
  if [[ "${ORCHESTRA_SEED_USER:-}" == "1" ]]; then
    echo "export UNIFY_KEY='$test_api_key'"
  fi

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
  if curl -s "http://127.0.0.1:${ORCHESTRA_PORT}/v0" &>/dev/null || \
     curl -s "http://127.0.0.1:${ORCHESTRA_PORT}/docs" &>/dev/null; then
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
    if [[ "${ORCHESTRA_SEED_USER:-}" == "1" ]]; then
      echo "export UNIFY_KEY='$test_api_key'"
    fi
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
      echo ""
      echo "User Seeding (set ORCHESTRA_SEED_USER=1 to enable):"
      echo "  ORCHESTRA_TEST_USER_ID  Test user ID (default: 'test-user-001')"
      echo "  ORCHESTRA_TEST_EMAIL    Test user email (default: 'test@debug.local')"
      echo "  UNIFY_KEY               API key for test user (default: 'local-test-api-key')"
      echo ""
      echo "Examples:"
      echo "  $0 start                              # Start orchestra"
      echo "  ORCHESTRA_PREFIX=myapp $0 start       # Start with custom prefix"
      echo "  ORCHESTRA_SEED_USER=1 $0 start        # Start and seed test user"
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
