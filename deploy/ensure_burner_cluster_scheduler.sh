#!/usr/bin/env bash
# Ensure the Cloud Scheduler jobs that sweep for burner-account clusters.
#
# Points at ``dry_run=true``, matching the deployed jobs, and must stay
# there until signup provenance is captured from the *browser*.
#
# The sweep's evidence is a group of never-paid accounts sharing a signup
# origin. Today every hosted signup reaches Orchestra through Console's
# Next.js server on an admin-key endpoint, so the request the origin is
# read from describes Console, not the person signing up: the user agent
# is that of Console's HTTP client and is byte-identical on every signup,
# and the IP is Console's Cloud Run egress. Every hosted email/password
# account therefore lands in one enormous shared-origin group, and at
# ``burner_cluster_min_accounts`` the sweep would suspend a set of
# unrelated customers as an abuse ring. OAuth signups record no origin at
# all, so they are merely invisible rather than incriminated.
#
# Enforcement becomes safe once Console forwards the real client IP and
# user agent explicitly and Orchestra records those instead. Until then
# BURNER_CLUSTER_DRY_RUN=false is not a tuning decision, it is an
# outage: read the run's ``without_provenance`` and ``largest_cluster``
# against ``considered`` and confirm the groups are made of people
# before arming it.
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
      sed -n '2,35p' "$0"
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
