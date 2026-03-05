"""Clean up old temporary files from the assistant media GCS bucket.

Temporary files are uploaded to the ``tmp/`` prefix when creating
Replicate animation predictions.  These files must remain available
long enough for Replicate to download them (typically < 30 minutes),
but should be removed afterwards to avoid storage costs.

The bucket does **not** have GCS lifecycle rules, so this routine
handles cleanup.  Files older than ``max_age_hours`` (default 2) are
deleted.

Scheduling Options:
1. GitHub Actions: .github/workflows/cleanup-temp-files.yml
   - Runs every 6 hours via cron: '0 */6 * * *'
   - Calls POST /v0/admin/temp-files/cleanup

2. Cloud Scheduler: Create a job to call the admin endpoint
   - Schedule: Every 6 hours
   - Endpoint: POST /v0/admin/temp-files/cleanup
   - Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>

3. Manual: Call the admin endpoint directly for one-off cleanup
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 2
TMP_PREFIX = "tmp/"


def cleanup_temp_files(max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> int:
    """
    Delete temporary files older than *max_age_hours* from the
    assistant media bucket's ``tmp/`` folder.

    Args:
        max_age_hours: Files older than this many hours are deleted.
            Defaults to 2 hours, which is safely beyond the 1-hour
            signed-URL expiration and typical Replicate processing time.

    Returns:
        Number of files deleted.
    """
    from orchestra.services.bucket_service import BucketService

    try:
        bucket_service = BucketService()
    except Exception as e:
        logger.error(f"Temp file cleanup: failed to initialise BucketService: {e}")
        raise

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    deleted = 0

    try:
        blobs = bucket_service.assistant_media_bucket.list_blobs(
            prefix=TMP_PREFIX,
        )

        for blob in blobs:
            # blob.updated is timezone-aware (UTC)
            if blob.updated and blob.updated < cutoff:
                try:
                    blob.delete()
                    deleted += 1
                except Exception as e:
                    logger.warning(
                        f"Temp file cleanup: failed to delete {blob.name}: {e}",
                    )

        if deleted > 0:
            logger.info(
                f"Temp file cleanup: removed {deleted} file(s) older than "
                f"{max_age_hours}h from gs://{bucket_service.assistant_media_bucket_name}/{TMP_PREFIX}",
            )
        else:
            logger.debug(
                "Temp file cleanup: no files older than " f"{max_age_hours}h to remove",
            )

        return deleted

    except Exception as e:
        logger.error(f"Temp file cleanup failed: {e}")
        raise
