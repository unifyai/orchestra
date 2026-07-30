#!/usr/bin/env bash
# Ensure the Cloud Scheduler jobs that periodically sweep for embedding
# partitions that need promoting out of the shared DEFAULT partition.
#
# Auth matches other Orchestra admin schedulers: static Bearer
# ``ORCHESTRA_ADMIN_KEY`` from Secret Manager (project ``gcp-project-saas``).
#
# Usage:
#   bash deploy/ensure_partition_promote_scheduler.sh
#   bash deploy/ensure_partition_promote_scheduler.sh --dry-run
#
# Idempotent: create or update staging + production jobs in us-central1.

set -euo pipefail

PROJECT="${GCP_PROJECT:-gcp-project-saas}"
LOCATION="${GCP_LOCATION:-us-central1}"
SCHEDULE="${PARTITION_PROMOTE_SWEEP_SCHEDULE:-0 5 * * *}"
RELATIVE_THRESHOLD="${PARTITION_PROMOTE_RELATIVE_THRESHOLD:-0.10}"
ATTEMPT_DEADLINE="${PARTITION_PROMOTE_SWEEP_DEADLINE:-120s}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

STAGING_URI="https://internal.example.com/v0/admin/partitioning/promote/start"
PROD_URI="https://api.unify.ai/v0/admin/partitioning/promote/start"

STAGING_JOB="orchestra-staging-partition-promote-sweep"
PROD_JOB="orchestra-partition-promote-sweep"

BODY=$(printf '{"relative_threshold":%s}' "$RELATIVE_THRESHOLD")

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
      echo "dry-run: would update $name -> $uri (schedule=$SCHEDULE body=$BODY)"
    else
      echo "dry-run: would create $name -> $uri (schedule=$SCHEDULE body=$BODY)"
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
  "Sweep for embedding partitions to promote out of the shared DEFAULT partition (staging)."

ensure_job \
  "$PROD_JOB" \
  "$PROD_URI" \
  "Sweep for embedding partitions to promote out of the shared DEFAULT partition (production)."
