#!/usr/bin/env bash
# =============================================================================
# stripe.sh - Manage Stripe CLI webhook forwarding for local E2E testing
# =============================================================================
#
# This script starts Stripe CLI to forward webhooks to local Orchestra,
# enabling E2E testing of payment flows with Stripe's test mode.
#
# Usage:
#   ./stripe.sh start     # Start webhook forwarding (foreground)
#   ./stripe.sh listen    # Alias for start
#   ./stripe.sh bg        # Start in background
#   ./stripe.sh stop      # Stop background listener
#   ./stripe.sh status    # Check if running
#   ./stripe.sh trigger   # Trigger test webhook events
#   ./stripe.sh setup     # Install and configure Stripe CLI
#
# Environment:
#   ORCHESTRA_PORT          FastAPI port (default: 8000)
#   STRIPE_WEBHOOK_PATH     Webhook endpoint path (default: /v0/webhooks/stripe)
#   STRIPE_DEVICE_NAME      Device name for Stripe CLI (default: orchestra-local)
#
# Prerequisites:
#   - Stripe CLI installed (brew install stripe/stripe-cli/stripe)
#   - Stripe account with test mode access
#   - Run 'stripe login' once to authenticate
#
# The webhook secret will be output on startup. Set it as:
#   export STRIPE_WEBHOOK_SECRET=whsec_xxx
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}"
STRIPE_WEBHOOK_PATH="${STRIPE_WEBHOOK_PATH:-/v0/webhooks/stripe}"
STRIPE_DEVICE_NAME="${STRIPE_DEVICE_NAME:-orchestra-local}"
STRIPE_PIDFILE="/tmp/stripe-listen-orchestra.pid"
STRIPE_LOGFILE="/tmp/stripe-listen-orchestra.log"
STRIPE_SECRET_FILE="/tmp/stripe-webhook-secret.txt"

WEBHOOK_URL="http://localhost:${ORCHESTRA_PORT}${STRIPE_WEBHOOK_PATH}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# =============================================================================
# Prerequisite Checks
# =============================================================================

check_stripe_cli() {
    if ! command -v stripe &>/dev/null; then
        log_error "Stripe CLI is not installed"
        echo ""
        echo "Install with:"
        echo "  macOS:  brew install stripe/stripe-cli/stripe"
        echo "  Linux:  curl -s https://packages.stripe.dev/api/security/keypair/stripe-cli-gpg/public | gpg --dearmor | sudo tee /usr/share/keyrings/stripe.gpg"
        echo "          echo \"deb [signed-by=/usr/share/keyrings/stripe.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main\" | sudo tee -a /etc/apt/sources.list.d/stripe.list"
        echo "          sudo apt update && sudo apt install stripe"
        echo ""
        echo "Then run: stripe login"
        return 1
    fi
    return 0
}

check_stripe_auth() {
    # Check if user is logged in by attempting to list a resource
    if ! stripe config --list 2>/dev/null | grep -q "test_mode"; then
        log_warn "Stripe CLI may not be authenticated"
        echo ""
        echo "Run: stripe login"
        echo ""
        return 1
    fi
    return 0
}

# =============================================================================
# Webhook Listener Management
# =============================================================================

is_listener_running() {
    if [[ -f "$STRIPE_PIDFILE" ]]; then
        local pid
        pid=$(cat "$STRIPE_PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi

    # Also check if any stripe listen process exists for our port
    if pgrep -f "stripe listen.*localhost:${ORCHESTRA_PORT}" &>/dev/null; then
        return 0
    fi

    return 1
}

extract_webhook_secret() {
    # Extract webhook secret from log output
    local logfile="$1"
    local max_wait=10
    local waited=0

    while (( waited < max_wait )); do
        if [[ -f "$logfile" ]]; then
            local secret
            secret=$(grep -o 'whsec_[a-zA-Z0-9]*' "$logfile" 2>/dev/null | head -1 || true)
            if [[ -n "$secret" ]]; then
                echo "$secret" > "$STRIPE_SECRET_FILE"
                echo "$secret"
                return 0
            fi
        fi
        sleep 1
        ((waited++)) || true
    done

    return 1
}

cmd_start() {
    echo "=============================================="
    echo "Starting Stripe Webhook Forwarding"
    echo "=============================================="
    echo ""

    if ! check_stripe_cli; then
        return 1
    fi

    if is_listener_running; then
        log_warn "Stripe listener is already running"
        if [[ -f "$STRIPE_SECRET_FILE" ]]; then
            echo ""
            echo "Webhook secret:"
            echo "  export STRIPE_WEBHOOK_SECRET=$(cat "$STRIPE_SECRET_FILE")"
        fi
        return 0
    fi

    log_info "Forwarding webhooks to: $WEBHOOK_URL"
    log_info "Device name: $STRIPE_DEVICE_NAME"
    echo ""
    echo -e "${CYAN}Press Ctrl+C to stop${NC}"
    echo ""

    # Run in foreground - user will see the webhook secret in output
    stripe listen \
        --forward-to "$WEBHOOK_URL" \
        --device-name "$STRIPE_DEVICE_NAME" \
        --events checkout.session.completed,invoice.payment_succeeded,invoice.paid,invoice.payment_failed,invoice.payment_action_required,charge.refunded,charge.refund.updated,charge.dispute.created,charge.dispute.funds_withdrawn,charge.dispute.closed,customer.tax_id.created,customer.tax_id.updated,customer.tax_id.deleted,customer.updated,review.opened,review.closed
}

cmd_bg() {
    echo "=============================================="
    echo "Starting Stripe Webhook Forwarding (Background)"
    echo "=============================================="
    echo ""

    if ! check_stripe_cli; then
        return 1
    fi

    if is_listener_running; then
        log_warn "Stripe listener is already running"
        if [[ -f "$STRIPE_SECRET_FILE" ]]; then
            echo ""
            echo "Webhook secret:"
            echo "  export STRIPE_WEBHOOK_SECRET=$(cat "$STRIPE_SECRET_FILE")"
        fi
        return 0
    fi

    log_info "Forwarding webhooks to: $WEBHOOK_URL"

    # Clear old log
    rm -f "$STRIPE_LOGFILE" "$STRIPE_SECRET_FILE"

    # Start in background
    nohup stripe listen \
        --forward-to "$WEBHOOK_URL" \
        --device-name "$STRIPE_DEVICE_NAME" \
        --events checkout.session.completed,invoice.payment_succeeded,invoice.paid,invoice.payment_failed,invoice.payment_action_required,charge.refunded,charge.refund.updated,charge.dispute.created,charge.dispute.funds_withdrawn,charge.dispute.closed,customer.tax_id.created,customer.tax_id.updated,customer.tax_id.deleted,customer.updated,review.opened,review.closed \
        > "$STRIPE_LOGFILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$STRIPE_PIDFILE"

    log_info "Started with PID $pid"
    log_info "Log file: $STRIPE_LOGFILE"

    # Wait for and extract webhook secret
    log_info "Waiting for webhook secret..."
    local secret
    if secret=$(extract_webhook_secret "$STRIPE_LOGFILE"); then
        log_success "Webhook secret obtained"
        echo ""
        echo "=============================================="
        echo "Set this environment variable:"
        echo ""
        echo "  export STRIPE_WEBHOOK_SECRET=$secret"
        echo ""
        echo "Or add to your .env file:"
        echo "  STRIPE_WEBHOOK_SECRET=$secret"
        echo "=============================================="
    else
        log_warn "Could not extract webhook secret automatically"
        echo "Check log file: tail -f $STRIPE_LOGFILE"
    fi

    return 0
}

cmd_stop() {
    if [[ -f "$STRIPE_PIDFILE" ]]; then
        local pid
        pid=$(cat "$STRIPE_PIDFILE")

        if kill -0 "$pid" 2>/dev/null; then
            log_info "Stopping Stripe listener (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 1

            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi

        rm -f "$STRIPE_PIDFILE"
    fi

    # Also kill any other stripe listen processes for our port
    pkill -f "stripe listen.*localhost:${ORCHESTRA_PORT}" 2>/dev/null || true

    rm -f "$STRIPE_SECRET_FILE"

    log_success "Stripe listener stopped"
}

cmd_status() {
    echo "Stripe Webhook Forwarder Status"
    echo "================================"
    echo ""

    echo -n "Stripe CLI: "
    if check_stripe_cli 2>/dev/null; then
        echo -e "${GREEN}installed${NC}"
    else
        echo -e "${RED}not installed${NC}"
    fi

    echo -n "Stripe Auth: "
    if check_stripe_auth 2>/dev/null; then
        echo -e "${GREEN}authenticated${NC}"
    else
        echo -e "${YELLOW}not authenticated (run: stripe login)${NC}"
    fi

    echo -n "Listener: "
    if is_listener_running; then
        echo -e "${GREEN}running${NC}"
        if [[ -f "$STRIPE_SECRET_FILE" ]]; then
            echo "  Webhook Secret: $(cat "$STRIPE_SECRET_FILE")"
        fi
    else
        echo -e "${RED}not running${NC}"
    fi

    echo ""
    echo "Configuration:"
    echo "  Webhook URL:  $WEBHOOK_URL"
    echo "  Device Name:  $STRIPE_DEVICE_NAME"
    echo "  Log File:     $STRIPE_LOGFILE"
    echo ""
}

cmd_trigger() {
    local event="${1:-checkout.session.completed}"

    log_info "Triggering test event: $event"

    case "$event" in
        checkout|checkout.session.completed)
            stripe trigger checkout.session.completed
            ;;
        invoice|invoice.paid)
            stripe trigger invoice.paid
            ;;
        tax_id|customer.tax_id.created)
            stripe trigger customer.tax_id.created
            ;;
        subscription|customer.subscription.created)
            stripe trigger customer.subscription.created
            ;;
        all)
            log_info "Triggering all relevant events..."
            stripe trigger checkout.session.completed
            stripe trigger invoice.paid
            stripe trigger customer.tax_id.created
            ;;
        *)
            stripe trigger "$event"
            ;;
    esac
}

cmd_setup() {
    echo "=============================================="
    echo "Stripe CLI Setup for Orchestra E2E Testing"
    echo "=============================================="
    echo ""

    # Check if already installed
    if check_stripe_cli 2>/dev/null; then
        log_success "Stripe CLI is already installed"
        stripe version
        echo ""
    else
        log_info "Installing Stripe CLI..."

        if [[ "$(uname)" == "Darwin" ]]; then
            if command -v brew &>/dev/null; then
                brew install stripe/stripe-cli/stripe
            else
                log_error "Homebrew not found. Install manually: https://stripe.com/docs/stripe-cli"
                return 1
            fi
        else
            # Linux installation
            curl -s https://packages.stripe.dev/api/security/keypair/stripe-cli-gpg/public | gpg --dearmor | sudo tee /usr/share/keyrings/stripe.gpg >/dev/null
            echo "deb [signed-by=/usr/share/keyrings/stripe.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main" | sudo tee /etc/apt/sources.list.d/stripe.list
            sudo apt update && sudo apt install -y stripe
        fi

        log_success "Stripe CLI installed"
    fi

    # Check authentication
    echo ""
    log_info "Checking Stripe authentication..."

    if ! check_stripe_auth 2>/dev/null; then
        echo ""
        log_info "Please authenticate with Stripe:"
        echo ""
        stripe login
    else
        log_success "Already authenticated with Stripe"
    fi

    echo ""
    echo "=============================================="
    echo "Setup Complete!"
    echo "=============================================="
    echo ""
    echo "Next steps:"
    echo "  1. Get your test API key from https://dashboard.stripe.com/test/apikeys"
    echo "  2. Add to .env: STRIPE_SECRET_KEY=sk_test_..."
    echo "  3. Start Orchestra: ./scripts/local.sh start"
    echo "  4. Start webhook forwarding: ./scripts/stripe.sh bg"
    echo "  5. Add webhook secret to .env: STRIPE_WEBHOOK_SECRET=whsec_..."
    echo ""
}

# =============================================================================
# Entry Point
# =============================================================================

main() {
    local cmd="${1:-help}"
    shift || true

    case "$cmd" in
        start|listen)
            cmd_start
            ;;
        bg|background)
            cmd_bg
            ;;
        stop)
            cmd_stop
            ;;
        status)
            cmd_status
            ;;
        trigger)
            cmd_trigger "$@"
            ;;
        setup|install)
            cmd_setup
            ;;
        help|-h|--help)
            echo "Usage: $0 [command]"
            echo ""
            echo "Commands:"
            echo "  start, listen    Start webhook forwarding (foreground)"
            echo "  bg, background   Start webhook forwarding (background)"
            echo "  stop             Stop background listener"
            echo "  status           Check listener status"
            echo "  trigger [event]  Trigger test webhook event"
            echo "  setup            Install and configure Stripe CLI"
            echo ""
            echo "Trigger events:"
            echo "  checkout         checkout.session.completed"
            echo "  invoice          invoice.paid"
            echo "  tax_id           customer.tax_id.created"
            echo "  subscription     customer.subscription.created"
            echo "  all              Trigger all above events"
            echo "  <any>            Pass any Stripe event type"
            echo ""
            echo "Environment Variables:"
            echo "  ORCHESTRA_PORT         FastAPI port (default: 8000)"
            echo "  STRIPE_WEBHOOK_PATH    Webhook path (default: /v0/webhooks/stripe)"
            echo "  STRIPE_DEVICE_NAME     Stripe device name (default: orchestra-local)"
            echo ""
            echo "Quick Start:"
            echo "  ./stripe.sh setup      # One-time setup"
            echo "  ./stripe.sh bg         # Start forwarding"
            echo "  # Copy webhook secret to .env"
            echo "  ./stripe.sh trigger checkout  # Test webhook"
            echo ""
            ;;
        *)
            log_error "Unknown command: $cmd"
            echo "Run '$0 help' for usage"
            exit 1
            ;;
    esac
}

main "$@"
