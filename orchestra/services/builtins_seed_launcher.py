"""Launch the Builtins artifacts seed worker as a Cloud Run Job.

The hosted seed sync is long-running (20-40 min) and must not block the public
HTTP trigger, which sits behind a load balancer that kills idle connections.
This module decouples trigger from execution: the request payload is persisted
to GCS and a dedicated Cloud Run Job runs the standalone worker against the
database, reporting progress through ``bootstrap_state``. Self-host/local
deployments leave the job unconfigured and run the sync inline instead.
"""

from __future__ import annotations

import json
import os

from orchestra.settings import settings


def builtins_seed_job_configured() -> bool:
    """Whether a Cloud Run Job is configured to run the seed worker.

    When set, the trigger endpoint launches the job and returns immediately;
    otherwise the sync runs inline in the calling process.
    """

    return bool(os.getenv("ORCHESTRA_BUILTINS_SEED_JOB_NAME"))


def _request_bucket() -> str:
    bucket = os.getenv("ORCHESTRA_BUILTINS_SEED_REQUEST_BUCKET") or os.getenv(
        "ORCHESTRA_GCP_BUCKET_NAME",
    )
    if not bucket:
        raise RuntimeError(
            "ORCHESTRA_BUILTINS_SEED_REQUEST_BUCKET or ORCHESTRA_GCP_BUCKET_NAME "
            "must be set to launch the Builtins seed job",
        )
    return bucket


def upload_seed_request(payload: dict) -> str:
    """Persist the worker request payload to GCS and return its ``gs://`` URI.

    The object key is keyed on ``run_id`` so a launch is idempotent and the
    worker can be pointed at it via ``--request-gcs-uri``.
    """

    from google.cloud import storage

    run_id = str(payload["run_id"])
    bucket_name = _request_bucket()
    object_name = f"builtins-seed-requests/{run_id}.json"
    request_uri = f"gs://{bucket_name}/{object_name}"
    body = {**payload, "artifact_kind": "integrations", "request_uri": request_uri}
    storage.Client().bucket(bucket_name).blob(object_name).upload_from_string(
        json.dumps(body, sort_keys=True),
        content_type="application/json",
    )
    return request_uri


def execute_seed_job(request_uri: str) -> None:
    """Trigger a Cloud Run Job execution pointing the worker at ``request_uri``."""

    from google.cloud import run_v2

    project = os.getenv("GCP_PROJECT_ID") or settings.gcp_project
    region = os.getenv("ORCHESTRA_BUILTINS_SEED_JOB_REGION", "europe-west3")
    job_name = os.environ["ORCHESTRA_BUILTINS_SEED_JOB_NAME"]

    client = run_v2.JobsClient()
    overrides = run_v2.RunJobRequest.Overrides(
        container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(
                args=["--request-gcs-uri", request_uri],
            ),
        ],
    )
    client.run_job(
        request=run_v2.RunJobRequest(
            name=client.job_path(project, region, job_name),
            overrides=overrides,
        ),
    )
