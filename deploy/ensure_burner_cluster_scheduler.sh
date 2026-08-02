#!/usr/bin/env bash
# Ensure the Cloud Scheduler jobs that sweep for burner-account clusters.
#
# Ships pointing at ``dry_run=true`` on purpose. The sweep suspends real
# customer accounts, and its evidence — several never-paid accounts
# sharing a signup origin, each having drained its grant — only becomes
# meaningful once signup provenance has been recorded for a while. Run it
# reporting-only first, read the flagged ids out of the run logs, and
# flip BURNER_CLUSTER_DRY_RUN=false when the matches look right.
#
# Auth matches other Orchestra admin schedulers: static Bearer
# ``ORCHESTRA_ADMIN_KEY`` from Secret Manager (project ``gcp-project-saas``).
#
# Usage:
#   bash deploy/ensure_burner_cluster_scheduler.sh
#   bash deploy/ensure_burner_cluster_scheduler.sh --dry-run
#   BURNER_CLUSTER_DRY_RUN=false bash deploy/ensure_burner_cluster_scheduler.sh
#
# Idempotent: create or update staging + production jobs in us-central1.
# Note ``--dry-run`` (do not touch Cloud Scheduler) is a different thing
# from BURNER_CLUSTER_DRY_RUN (the sweep reports without freezing).

set -euo pipefail

PROJECT="${GCP_PROJECT:-gcp-project-saas}"
LOCATION="${GCP_LOCATION:-us-central1}"
# Daily, after the other billing routines have settled.
SCHEDULE="${BURNER_CLUSTER_SWEEP_SCHEDULE:-30 5 * * *}"
SWEEP_DRY_RUN="${BURNER_CLUSTER_DRY_RUN:-true}"
ATTEMPT_DEADLINE="${BURNER_CLUSTER_SWEEP_DEADLINE:-120s}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

STAGING_URI="https://internal.example.com/v0/admin/billing/burner-cluster-freeze-sweep?dry_run=${SWEEP_DRY_RUN}"
PROD_URI="https://api.unify.ai/v0/admin/billing/burner-cluster-freeze-sweep?dry_run=${SWEEP_DRY_RUN}"

STAGING_JOB="orchestra-staging-burner-cluster-sweep"
PROD_JOB="orchestra-burner-cluster-sweep"

BODY="{}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is required" >&2
  exit 1
fi

ADMIN_KEY="$(
  gcloud secrets versions access latest \
    --secret=ORCHESTRA_ADMIN_KEY \
    --project="$PROJECT"
)"
if [[ -z "${ADMIN_KEY}" ]]; then
  echo "ORCHESTRA_ADMIN_KEY secret is empty" >&2
  exit 1
fi

ensure_job() {
  local name="$1"
  local uri="$2"
  local description="$3"
  local exists=0
  local headers="Content-Type=application/json,User-Agent=Google-Cloud-Scheduler,Authorization=Bearer ${ADMIN_KEY}"

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

  if [[ "$exists" -eq 1 ]]; then
    gcloud scheduler jobs update http "$name" \
      --project="$PROJECT" \
      --location="$LOCATION" \
      --schedule="$SCHEDULE" \
      --time-zone=Etc/UTC \
      --uri="$uri" \
      --http-method=POST \
      --update-headers="$headers" \
      --message-body="$BODY" \
      --attempt-deadline="$ATTEMPT_DEADLINE" \
      --max-retry-attempts=1 \
      --min-backoff=30s \
      --max-backoff=300s \
      --description="$description" \
      >/dev/null
    echo "updated $name -> $uri"
  else
    gcloud scheduler jobs create http "$name" \
      --project="$PROJECT" \
      --location="$LOCATION" \
      --schedule="$SCHEDULE" \
      --time-zone=Etc/UTC \
      --uri="$uri" \
      --http-method=POST \
      --headers="$headers" \
      --message-body="$BODY" \
      --attempt-deadline="$ATTEMPT_DEADLINE" \
      --max-retry-attempts=1 \
      --min-backoff=30s \
      --max-backoff=300s \
      --description="$description" \
      >/dev/null
    echo "created $name -> $uri"
  fi
}

ensure_job \
  "$STAGING_JOB" \
  "$STAGING_URI" \
  "Freeze never-paid accounts farming credits in a signup-origin cluster (staging)."

ensure_job \
  "$PROD_JOB" \
  "$PROD_URI" \
  "Freeze never-paid accounts farming credits in a signup-origin cluster (production)."
