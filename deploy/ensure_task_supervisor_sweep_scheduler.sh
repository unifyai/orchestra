#!/usr/bin/env bash
# Ensure the Cloud Scheduler jobs that drive the task supervisor sweep
# (orchestra/routines/task_supervisor_sweep.py) in staging and production.
#
# Auth is OIDC as the dedicated ``task-supervisor-sweep`` service account,
# matched server-side against ``CLOUD_SCHEDULER_SERVICE_ACCOUNT``. Never a
# static admin key in headers: the Cloud Scheduler API returns headers in
# plaintext to anyone who can read the job.
#
# Usage:
#   bash deploy/ensure_task_supervisor_sweep_scheduler.sh
#   bash deploy/ensure_task_supervisor_sweep_scheduler.sh --dry-run
#
# Idempotent: create or update staging + production jobs in us-central1.
# The staging service must carry CLOUD_SCHEDULER_SERVICE_ACCOUNT (set in
# deploy/cloudbuild_staging.yaml) before the staging job can authenticate;
# until that deploy lands the job fails 401 harmlessly on each tick.

set -euo pipefail

PROJECT="${GCP_PROJECT:-gcp-project-saas}"
LOCATION="${GCP_LOCATION:-us-central1}"
SCHEDULE="${TASK_SUPERVISOR_SWEEP_SCHEDULE:-*/15 * * * *}"
ATTEMPT_DEADLINE="${TASK_SUPERVISOR_SWEEP_DEADLINE:-540s}"
SERVICE_ACCOUNT="task-supervisor-sweep@${PROJECT}.iam.gserviceaccount.com"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

STAGING_URI="https://internal.example.com/v0/admin/task-supervisor/sweep"
PROD_URI="https://api.unify.ai/v0/admin/task-supervisor/sweep"

STAGING_JOB="orchestra-staging-task-supervisor-sweep"
PROD_JOB="orchestra-production-task-supervisor-sweep"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is required" >&2
  exit 1
fi

ensure_job() {
  local name="$1"
  local uri="$2"
  local description="$3"
  local exists=0

  if gcloud scheduler jobs describe "$name" \
    --project="$PROJECT" \
    --location="$LOCATION" >/dev/null 2>&1; then
    exists=1
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ "$exists" -eq 1 ]]; then
      echo "dry-run: would update $name -> $uri (schedule=$SCHEDULE)"
    else
      echo "dry-run: would create $name -> $uri (schedule=$SCHEDULE)"
    fi
    return 0
  fi

  local verb="create"
  [[ "$exists" -eq 1 ]] && verb="update"
  gcloud scheduler jobs "$verb" http "$name" \
    --project="$PROJECT" \
    --location="$LOCATION" \
    --schedule="$SCHEDULE" \
    --time-zone=Etc/UTC \
    --uri="$uri" \
    --http-method=POST \
    --oidc-service-account-email="$SERVICE_ACCOUNT" \
    --oidc-token-audience="$uri" \
    --attempt-deadline="$ATTEMPT_DEADLINE" \
    --description="$description" \
    >/dev/null
  echo "${verb}d $name -> $uri"
}

ensure_job \
  "$STAGING_JOB" \
  "$STAGING_URI" \
  "Floor under the task recurrence relay chain: re-projects the open head for every enabled, armed definition (see orchestra/routines/task_supervisor_sweep.py). Fails 401 harmlessly until the staging service carries CLOUD_SCHEDULER_SERVICE_ACCOUNT."

ensure_job \
  "$PROD_JOB" \
  "$PROD_URI" \
  "Floor under the task recurrence relay chain: re-projects the open head for every enabled, armed definition (see orchestra/routines/task_supervisor_sweep.py)."
