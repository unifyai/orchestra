"""
Background worker for processing embedding generation queue.

This worker:
1. Polls the embedding_queue table for pending items
2. Uses FOR UPDATE SKIP LOCKED for atomic, multi-worker safe claiming
3. Processes embeddings in batches (2048 - OpenAI recommended size)
4. Calls OpenAI API for embedding generation
5. Inserts embeddings into the database (with upsert for soft-deleted rows)
6. Handles retries for failed embeddings
7. Respects both time and size bounds for processing

Usage:
    python -m orchestra.workers.embedding_worker

Environment Variables:
    ORCHESTRA_OPENAI_API_KEY: OpenAI API key for embedding generation
    DB_HOST, DB_USER, DB_PASS, DB_NAME: Database connection parameters

The worker can be deployed as:
- Cloud Run Job (triggered by Pub/Sub or Cloud Scheduler)
- Kubernetes CronJob
- Standalone daemon process
"""

import logging
import os
import signal
import sys
import time
from collections import defaultdict
from typing import List, NamedTuple, Optional

from sqlalchemy import create_engine, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

# OpenAI API batching - recommended batch size for embedding generation
BATCH_SIZE = 2048

# Retry behavior - max attempts before marking as 'failed'
MAX_RETRIES = 3

# Processing bounds - defaults for process_pending_embeddings()
DEFAULT_LIMIT = 5000  # Default max items per invocation
DEFAULT_TIME_LIMIT_SECONDS = 300  # Default time bound (5 minutes)

# Crash recovery - reset items stuck in 'processing' longer than this
STALE_THRESHOLD_MINUTES = 10

# Daemon mode only - sleep interval when queue is empty
POLL_INTERVAL_SECONDS = 30

# Global shutdown flag for graceful termination
shutdown_flag = False


class QueueItem(NamedTuple):
    """Represents a claimed queue item."""

    id: int
    ref_id: int
    key: str
    text: str
    model: str
    dimensions: Optional[int]
    status: str
    retry_count: int
    error_message: Optional[str]
    created_at: object


# SQL query for atomic claiming with FOR UPDATE SKIP LOCKED
# This prevents race conditions when multiple workers run concurrently
CLAIM_QUERY = """
WITH claimable AS (
    SELECT id FROM embedding_queue
    WHERE status = 'pending'
      AND retry_count < :max_retries
    ORDER BY created_at
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
UPDATE embedding_queue q
SET status = 'processing',
    processing_started_at = NOW()
FROM claimable c
WHERE q.id = c.id
RETURNING q.id, q.ref_id, q.key, q.text, q.model, q.dimensions,
          q.status, q.retry_count, q.error_message, q.created_at
"""


def signal_handler(signum, frame):
    """Handle graceful shutdown."""
    global shutdown_flag
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    shutdown_flag = True


def get_db_session() -> Session:
    """Create database session for worker."""
    # Import here to avoid circular imports
    from orchestra.settings import settings

    if settings.use_cloud_sql:
        try:
            from google.cloud.sql.connector import Connector

            instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME", "")
            db_user = os.environ.get("DB_USER", settings.db_user)
            db_pass = os.environ.get("DB_PASS", settings.db_pass)
            db_name = os.environ.get("DB_NAME", settings.db_base)

            connector = Connector()

            def get_conn():
                return connector.connect(
                    instance_connection_name,
                    "pg8000",
                    user=db_user,
                    password=db_pass,
                    db=db_name,
                )

            engine = create_engine("postgresql+pg8000://", creator=get_conn)
        except ImportError:
            logger.warning(
                "google-cloud-sql-connector not available, falling back to direct connection",
            )
            engine = create_engine(str(settings.db_url), pool_pre_ping=True)
    else:
        engine = create_engine(str(settings.db_url), pool_pre_ping=True)

    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    return SessionLocal()


def reset_stale_processing_items(session: Session) -> int:
    """
    Reset queue items stuck in 'processing' state for too long.

    This handles worker crashes by resetting items that have been
    in 'processing' state for longer than STALE_THRESHOLD_MINUTES.

    Uses processing_started_at (when item was claimed) NOT created_at (when queued),
    to avoid incorrectly resetting items that were queued long ago but just claimed.

    Args:
        session: Database session

    Returns:
        Number of items reset
    """
    result = session.execute(
        text(
            """
            UPDATE embedding_queue
            SET status = 'pending',
                processing_started_at = NULL
            WHERE status = 'processing'
              AND processing_started_at IS NOT NULL
              AND processing_started_at < NOW() - INTERVAL ':minutes minutes'
        """.replace(
                ":minutes",
                str(STALE_THRESHOLD_MINUTES),
            ),
        ),
    )
    session.commit()
    reset_count = result.rowcount
    if reset_count > 0:
        logger.warning(
            f"Reset {reset_count} stale items from 'processing' back to 'pending'",
        )
    return reset_count


def claim_pending_batch(session: Session, limit: int) -> List[QueueItem]:
    """
    Atomically claim a batch of pending queue items.

    Uses FOR UPDATE SKIP LOCKED to prevent race conditions when
    multiple workers are running concurrently.

    Args:
        session: Database session
        limit: Maximum number of items to claim

    Returns:
        List of claimed QueueItem objects
    """
    result = session.execute(
        text(CLAIM_QUERY),
        {"max_retries": MAX_RETRIES, "limit": limit},
    )
    session.commit()

    rows = result.fetchall()
    return [
        QueueItem(
            id=row[0],
            ref_id=row[1],
            key=row[2],
            text=row[3],
            model=row[4],
            dimensions=row[5],
            status=row[6],
            retry_count=row[7],
            error_message=row[8],
            created_at=row[9],
        )
        for row in rows
    ]


def process_embedding_batch(session: Session, batch: List[QueueItem]) -> int:
    """
    Process a batch of claimed embeddings.

    Items are already marked as 'processing' by claim_pending_batch(),
    so this function just generates embeddings and handles success/failure.

    Args:
        session: Database session
        batch: List of QueueItem objects to process (already claimed)

    Returns:
        Number of successfully processed items
    """
    # Import models here to avoid circular imports at module level
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue
    from orchestra.web.api.log.python2SQL.helpers import _get_embeddings_batch

    if not batch:
        return 0

    batch_ids = [item.id for item in batch]

    try:
        # Extract texts and generate embeddings
        texts = [item.text for item in batch]
        model = batch[0].model  # All items in batch have same model
        dimensions = batch[0].dimensions

        logger.info(f"Generating {len(texts)} embeddings for model {model}")

        # Call OpenAI API
        embeddings = _get_embeddings_batch(texts, model, dimensions)

        # Prepare embedding objects for bulk insert
        embedding_objects = [
            {
                "ref_id": batch[i].ref_id,
                "key": batch[i].key,
                "model": batch[i].model,
                "vector": embeddings[i],
                "is_deleted": False,
            }
            for i in range(len(batch))
        ]

        # Bulk upsert embeddings (handles soft-deleted rows by resurrecting them)
        stmt = insert(Embedding).values(embedding_objects)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_embedding",
            set_={
                "vector": stmt.excluded.vector,
                "is_deleted": False,  # Resurrect soft-deleted embeddings
            },
        )
        session.execute(stmt)

        # Delete processed items from queue
        session.query(EmbeddingQueue).filter(
            EmbeddingQueue.id.in_(batch_ids),
        ).delete(synchronize_session=False)

        session.commit()
        logger.info(f"Successfully processed {len(batch)} embeddings")
        return len(batch)

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to process batch: {e}", exc_info=True)

        # Update retry count and status for each item
        for item in batch:
            new_retry_count = item.retry_count + 1
            new_status = "failed" if new_retry_count >= MAX_RETRIES else "pending"

            session.execute(
                update(EmbeddingQueue)
                .where(EmbeddingQueue.id == item.id)
                .values(
                    status=new_status,
                    retry_count=new_retry_count,
                    error_message=str(e)[:500],  # Truncate error message
                    # Clear processing_started_at when returning to pending
                    processing_started_at=None
                    if new_status == "pending"
                    else item.created_at,
                ),
            )
        session.commit()
        return 0


def get_queue_metrics(session: Session) -> dict:
    """Get current queue metrics for monitoring."""
    from sqlalchemy import func

    from orchestra.db.models.orchestra_models import EmbeddingQueue

    # Count by status
    status_counts = dict(
        session.query(EmbeddingQueue.status, func.count(EmbeddingQueue.id))
        .group_by(EmbeddingQueue.status)
        .all(),
    )

    # Get retry stats
    retry_stats = (
        session.query(
            func.avg(EmbeddingQueue.retry_count),
            func.max(EmbeddingQueue.retry_count),
        )
        .filter(EmbeddingQueue.status == "failed")
        .first()
    )

    return {
        "pending": status_counts.get("pending", 0),
        "processing": status_counts.get("processing", 0),
        "failed": status_counts.get("failed", 0),
        "avg_retries_on_failed": float(retry_stats[0] or 0),
        "max_retries_on_failed": retry_stats[1] or 0,
    }


def process_pending_embeddings(
    session: Session,
    limit: int = DEFAULT_LIMIT,
    max_time_seconds: int = DEFAULT_TIME_LIMIT_SECONDS,
) -> dict:
    """
    Process pending embeddings with both time and size bounding.

    This function:
    1. Resets any stale items stuck in 'processing' state
    2. Atomically claims batches using FOR UPDATE SKIP LOCKED
    3. Processes batches until limit or time bound is reached
    4. Returns detailed metrics about the processing run

    Args:
        session: Database session
        limit: Maximum number of embeddings to process
        max_time_seconds: Maximum time to spend processing (in seconds)

    Returns:
        Dictionary with processing metrics
    """

    start_time = time.time()

    # Reset any stale items stuck in 'processing' state (crash recovery)
    stale_reset = reset_stale_processing_items(session)

    # Log queue metrics before processing
    queue_metrics = get_queue_metrics(session)
    logger.info(
        f"Queue metrics: pending={queue_metrics['pending']}, "
        f"processing={queue_metrics['processing']}, "
        f"failed={queue_metrics['failed']}, "
        f"stale_reset={stale_reset}",
    )

    total_processed = 0
    total_errors = 0
    time_limit_reached = False
    size_limit_reached = False

    while total_processed < limit:
        # Check time bound
        elapsed = time.time() - start_time
        if elapsed >= max_time_seconds:
            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
            time_limit_reached = True
            break

        # Check shutdown flag
        if shutdown_flag:
            logger.info("Shutdown requested, stopping processing")
            break

        # Calculate batch size (don't exceed remaining limit)
        remaining = limit - total_processed
        batch_size = min(BATCH_SIZE, remaining)

        # Atomically claim a batch of pending items
        claimed = claim_pending_batch(session, batch_size)

        if not claimed:
            logger.debug("No more pending embeddings to process")
            break

        logger.info(f"Claimed {len(claimed)} items for processing")

        # Group by (model, dimensions) - can't batch different models together
        by_model = defaultdict(list)
        for item in claimed:
            key = (item.model, item.dimensions)
            by_model[key].append(item)

        # Process each model's embeddings
        for (model, dimensions), items in by_model.items():
            logger.info(f"Processing {len(items)} embeddings for model {model}")

            # Process in sub-batches if needed (though usually same model)
            for i in range(0, len(items), BATCH_SIZE):
                if shutdown_flag:
                    break

                sub_batch = items[i : i + BATCH_SIZE]
                processed = process_embedding_batch(session, sub_batch)

                if processed > 0:
                    total_processed += processed
                else:
                    total_errors += len(sub_batch)

        # Check if we've hit the size limit
        if total_processed >= limit:
            size_limit_reached = True
            break

    # Log final metrics
    duration = time.time() - start_time
    throughput = total_processed / duration if duration > 0 else 0
    error_rate = (
        total_errors / (total_processed + total_errors)
        if (total_processed + total_errors) > 0
        else 0
    )

    logger.info(
        f"Processing complete: processed={total_processed}, "
        f"errors={total_errors}, "
        f"duration={duration:.2f}s, "
        f"throughput={throughput:.1f}/s, "
        f"error_rate={error_rate:.2%}, "
        f"time_limit_reached={time_limit_reached}, "
        f"size_limit_reached={size_limit_reached}",
    )

    # Return detailed metrics
    return {
        "processed": total_processed,
        "errors": total_errors,
        "duration": duration,
        "throughput": throughput,
        "error_rate": error_rate,
        "stale_reset": stale_reset,
        "time_limit_reached": time_limit_reached,
        "size_limit_reached": size_limit_reached,
    }


def run_once(session: Session) -> int:
    """
    Run one processing cycle.

    Args:
        session: Database session

    Returns:
        Number of embeddings processed
    """
    try:
        result = process_pending_embeddings(session)
        return result.get("processed", 0)
    except Exception as e:
        logger.error(f"Error in processing cycle: {e}", exc_info=True)
        return 0


def main():
    """Main entry point for the embedding worker."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Starting embedding worker")

    try:
        session = get_db_session()

        while not shutdown_flag:
            try:
                result = process_pending_embeddings(session)
                processed = result.get("processed", 0)

                if processed == 0:
                    # No work to do, sleep for a bit
                    logger.debug(
                        f"No pending work, sleeping for {POLL_INTERVAL_SECONDS} seconds",
                    )
                    time.sleep(POLL_INTERVAL_SECONDS)
                else:
                    logger.info(f"Processed {processed} embeddings")

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(10)  # Back off on error

        logger.info("Embedding worker shutting down")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if "session" in locals():
            session.close()


if __name__ == "__main__":
    main()
