"""Trigger the embedding-partition promote Cloud Run Job on demand.

Mirrors ``builtins_seed_launcher``: the promote worker
(``MAINTENANCE_MODE=promote``, see ``orchestra.workers.index_maintenance``)
already finds and promotes its own candidates from the DEFAULT partition, so
triggering it only needs a bare job execution -- no request payload to stage.
Self-host/local deployments leave the job unconfigured and the periodic sweep
endpoint simply reports it can't trigger anything.
"""

from __future__ import annotations

import os

from orchestra.settings import settings


def partition_promote_job_configured() -> bool:
    """Whether a Cloud Run Job is configured to run the partition-promote worker."""

    return bool(os.getenv("ORCHESTRA_PARTITION_PROMOTE_JOB_NAME"))


def execute_partition_promote_job() -> None:
    """Trigger a Cloud Run Job execution of the partition-promote worker."""

    from google.cloud import run_v2

    project = os.getenv("GCP_PROJECT_ID") or settings.gcp_project
    region = os.getenv("ORCHESTRA_PARTITION_PROMOTE_JOB_REGION", "us-central1")
    job_name = os.environ["ORCHESTRA_PARTITION_PROMOTE_JOB_NAME"]

    client = run_v2.JobsClient()
    client.run_job(
        request=run_v2.RunJobRequest(
            name=client.job_path(project, region, job_name),
        ),
    )
