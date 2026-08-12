#!/usr/bin/env bash
# Ensure the Cloud Scheduler jobs that report burner-account clusters.
#
# The job reports and nothing else. It names never-paid accounts that
# each drained a grant from one signup origin inside a short window, and
# posts them to the billing Discord for a person to judge. Suspending one
# is done by hand afterwards, through POST /admin/billing/freeze with
# reason ``abuse_fingerprint``.
#
# There is deliberately no switch here for acting automatically. The
# evidence is circumstantial however many conditions are stacked — an
# office, a VPN exit and a university NAT all look like this — and the
# cost of being wrong is a customer locked out of an account they were
# about to pay for. It was briefly wired to an automatic suspension and
# came within one signup of freezing five strangers who shared nothing
# but Console's HTTP client.
#
# Auth matches other Orchestra admin schedulers: static Bearer
# ``ORCHESTRA_ADMIN_KEY`` from Secret Manager (project ``gcp-project-saas``).
#
# Usage:
#   bash deploy/ensure_burner_cluster_scheduler.sh
#   bash deploy/ensure_burner_cluster_scheduler.sh --dry-run
#
# Idempotent: create or update staging + production jobs in us-central1.
# ``--dry-run`` here means "do not touch Cloud Scheduler"; the job it
# manages has no such mode, because it never changes anything.

set -euo pipefail

PROJECT="${GCP_PROJECT:-gcp-project-saas}"
LOCATION="${GCP_LOCATION:-us-central1}"
# Daily, after the other billing routines have settled.
SCHEDULE="${BURNER_CLUSTER_SWEEP_SCHEDULE:-30 5 * * *}"
ATTEMPT_DEADLINE="${BURNER_CLUSTER_SWEEP_DEADLINE:-120s}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

STAGING_URI="https://internal.example.com/v0/admin/billing/burner-cluster-report"
PROD_URI="https://api.unify.ai/v0/admin/billing/burner-cluster-report"

STAGING_JOB="orchestra-staging-burner-cluster-report"
PROD_JOB="orchestra-burner-cluster-report"

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
  "Report never-paid accounts farming credits in a signup-origin cluster (staging)."

ensure_job \
  "$PROD_JOB" \
  "$PROD_URI" \
  "Report never-paid accounts farming credits in a signup-origin cluster (production)."
