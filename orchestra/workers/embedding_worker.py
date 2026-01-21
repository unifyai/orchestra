"""
Background worker for processing embedding generation queue.

This worker:
1. Polls the embedding_queue table for pending items
2. Uses FOR UPDATE SKIP LOCKED for atomic, multi-worker safe claiming
3. Generates embeddings via OpenAI API in batches of 2048 (API limit)
4. Performs a SINGLE bulk insert per invocation (not per-batch)
5. Handles retries for failed embeddings
6. Respects both time and size bounds for processing

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
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

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

# --- OpenAI API Limits ---
# Maximum batch size for a single OpenAI embedding API call (recommended by OpenAI)
OPENAI_EMBEDDING_BATCH_SIZE = 2048

# --- Retry Behavior ---
# Maximum retry attempts before marking a queue item as 'failed'
MAX_RETRY_ATTEMPTS = 3

# --- Crash Recovery ---
# Items stuck in 'processing' longer than this will be reset to 'pending'
STALE_ITEM_TIMEOUT_MINUTES = 5

# --- Daemon Mode (standalone worker) ---
# Sleep interval when queue is empty (only used in daemon mode)
DAEMON_POLL_INTERVAL_SECONDS = 30

# --- Default Processing Bounds ---
# These defaults are used when calling process_pending_embeddings() directly.
# The API endpoint can override these with its own defaults/limits.
DEFAULT_MAX_ITEMS_PER_INVOCATION = 5000  # Max items to process in one call
DEFAULT_MAX_TIME_SECONDS = 170  # Max time before stopping (~3 min with 10s buffer)

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


@dataclass
class GeneratedEmbedding:
    """Represents an embedding ready for database insertion."""

    queue_item_id: int
    ref_id: int
    key: str
    model: str
    vector: list
    is_deleted: bool = False


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
    in 'processing' state for longer than STALE_ITEM_TIMEOUT_MINUTES.

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
                str(STALE_ITEM_TIMEOUT_MINUTES),
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
        {"max_retries": MAX_RETRY_ATTEMPTS, "limit": limit},
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


def generate_embeddings_for_items(
    items: List[QueueItem],
) -> Tuple[List[GeneratedEmbedding], List[QueueItem]]:
    """
    Generate embeddings for a list of queue items using OpenAI API.

    This function batches items by (model, dimensions) and calls the OpenAI API
    in chunks of OPENAI_EMBEDDING_BATCH_SIZE (2048).

    Args:
        items: List of QueueItem objects to generate embeddings for

    Returns:
        Tuple of (successful_embeddings, failed_items)
    """
    from orchestra.web.api.log.python2SQL.helpers import _get_embeddings_batch

    if not items:
        return [], []

    # Group by (model, dimensions) - can't batch different models together
    by_model: Dict[Tuple[str, Optional[int]], List[QueueItem]] = defaultdict(list)
    for item in items:
        key = (item.model, item.dimensions)
        by_model[key].append(item)

    successful_embeddings: List[GeneratedEmbedding] = []
    failed_items: List[QueueItem] = []

    for (model, dimensions), model_items in by_model.items():
        logger.info(
            f"Generating embeddings for {len(model_items)} items with model {model}",
        )

        # Process in OpenAI-compatible batches (2048 max)
        for batch_start in range(0, len(model_items), OPENAI_EMBEDDING_BATCH_SIZE):
            batch = model_items[batch_start : batch_start + OPENAI_EMBEDDING_BATCH_SIZE]
            texts = [item.text for item in batch]

            try:
                # Call OpenAI API
                embeddings = _get_embeddings_batch(texts, model, dimensions)

                # Create GeneratedEmbedding objects
                for i, item in enumerate(batch):
                    successful_embeddings.append(
                        GeneratedEmbedding(
                            queue_item_id=item.id,
                            ref_id=item.ref_id,
                            key=item.key,
                            model=item.model,
                            vector=embeddings[i],
                            is_deleted=False,
                        ),
                    )

                logger.info(f"Generated {len(batch)} embeddings for model {model}")

            except Exception as e:
                logger.error(
                    f"Failed to generate embeddings for batch: {e}",
                    exc_info=True,
                )
                # Mark entire batch as failed
                failed_items.extend(batch)

    return successful_embeddings, failed_items


def bulk_insert_embeddings(
    session: Session,
    embeddings: List[GeneratedEmbedding],
    queue_item_ids: List[int],
) -> int:
    """
    Perform a single bulk upsert of all generated embeddings.

    This is more efficient than inserting per-batch because:
    1. Single transaction for all embeddings
    2. Single index update operation
    3. Reduced round-trips to database

    Args:
        session: Database session
        embeddings: List of GeneratedEmbedding objects to insert
        queue_item_ids: IDs of queue items to remove after successful insert

    Returns:
        Number of embeddings inserted
    """
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue

    if not embeddings:
        return 0

    try:
        # Prepare embedding objects for bulk insert
        embedding_dicts = [
            {
                "ref_id": emb.ref_id,
                "key": emb.key,
                "model": emb.model,
                "vector": emb.vector,
                "is_deleted": False,
            }
            for emb in embeddings
        ]

        # Bulk upsert embeddings (handles soft-deleted rows by resurrecting them)
        stmt = insert(Embedding).values(embedding_dicts)
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
            EmbeddingQueue.id.in_(queue_item_ids),
        ).delete(synchronize_session=False)

        session.commit()
        logger.info(f"Bulk inserted {len(embeddings)} embeddings")
        return len(embeddings)

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to bulk insert embeddings: {e}", exc_info=True)
        raise


def mark_items_as_failed(
    session: Session,
    items: List[QueueItem],
    error_message: str,
) -> None:
    """
    Update failed items with incremented retry count.

    Items that exceed MAX_RETRY_ATTEMPTS are marked as 'failed'.
    Otherwise, they're returned to 'pending' for retry.

    Args:
        session: Database session
        items: List of QueueItem objects that failed
        error_message: Error message to store
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    for item in items:
        new_retry_count = item.retry_count + 1
        new_status = "failed" if new_retry_count >= MAX_RETRY_ATTEMPTS else "pending"

        session.execute(
            update(EmbeddingQueue)
            .where(EmbeddingQueue.id == item.id)
            .values(
                status=new_status,
                retry_count=new_retry_count,
                error_message=error_message[:500],  # Truncate
                processing_started_at=None if new_status == "pending" else None,
            ),
        )
    session.commit()


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
    limit: int = DEFAULT_MAX_ITEMS_PER_INVOCATION,
    max_time_seconds: int = DEFAULT_MAX_TIME_SECONDS,
) -> dict:
    """
    Process pending embeddings with time and size bounding.

    This function:
    1. Resets any stale items stuck in 'processing' state (crash recovery)
    2. Atomically claims items using FOR UPDATE SKIP LOCKED
    3. Generates all embeddings (in OpenAI-compatible batches of 2048)
    4. Performs a SINGLE bulk insert for all generated embeddings
    5. Returns detailed metrics about the processing run

    The key optimization is that we generate embeddings in multiple API calls
    (limited by OpenAI's 2048 batch size), but only do ONE database insert
    at the end of the invocation. This reduces database load and transaction
    overhead significantly for large batches.

    Args:
        session: Database session
        limit: Maximum number of embeddings to process (default: 5000)
        max_time_seconds: Maximum time to spend processing in seconds (default: 280)

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

    # Collect all items to process in this invocation
    all_claimed_items: List[QueueItem] = []
    time_limit_reached = False
    size_limit_reached = False

    # Phase 1: Claim items up to the limit (or until queue is empty)
    while len(all_claimed_items) < limit:
        # Check time bound
        elapsed = time.time() - start_time
        if elapsed >= max_time_seconds:
            logger.info(f"Time limit reached during claiming ({elapsed:.1f}s)")
            time_limit_reached = True
            break

        # Check shutdown flag
        if shutdown_flag:
            logger.info("Shutdown requested, stopping")
            break

        # Calculate how many more items we can claim
        remaining = limit - len(all_claimed_items)
        # Don't claim more than OPENAI_EMBEDDING_BATCH_SIZE at a time for efficiency
        batch_size = min(OPENAI_EMBEDDING_BATCH_SIZE, remaining)

        # Atomically claim a batch of pending items
        claimed = claim_pending_batch(session, batch_size)

        if not claimed:
            logger.debug("No more pending embeddings to claim")
            break

        all_claimed_items.extend(claimed)
        logger.info(
            f"Claimed {len(claimed)} items (total: {len(all_claimed_items)}/{limit})",
        )

        if len(all_claimed_items) >= limit:
            size_limit_reached = True
            break

    if not all_claimed_items:
        duration = time.time() - start_time
        return {
            "processed": 0,
            "errors": 0,
            "duration": duration,
            "throughput": 0,
            "error_rate": 0,
            "stale_reset": stale_reset,
            "time_limit_reached": time_limit_reached,
            "size_limit_reached": size_limit_reached,
        }

    logger.info(f"Total claimed: {len(all_claimed_items)} items")

    # Phase 2: Generate all embeddings (multiple API calls, batched by 2048)
    successful_embeddings, failed_items = generate_embeddings_for_items(
        all_claimed_items,
    )

    # Phase 3: Single bulk insert for all successful embeddings
    total_processed = 0
    total_errors = len(failed_items)

    if successful_embeddings:
        try:
            # Get queue item IDs for successful embeddings
            successful_queue_ids = [emb.queue_item_id for emb in successful_embeddings]

            total_processed = bulk_insert_embeddings(
                session,
                successful_embeddings,
                successful_queue_ids,
            )
        except Exception as e:
            logger.error(f"Bulk insert failed: {e}", exc_info=True)
            # All items that were supposedly successful are now failed
            failed_items.extend(
                [
                    item
                    for item in all_claimed_items
                    if item.id in [emb.queue_item_id for emb in successful_embeddings]
                ],
            )
            total_errors = len(failed_items)
            total_processed = 0

    # Phase 4: Handle failed items
    if failed_items:
        mark_items_as_failed(session, failed_items, "Embedding generation failed")

    # Calculate final metrics
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
    """Main entry point for the embedding worker (daemon mode)."""
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
                        f"No pending work, sleeping for {DAEMON_POLL_INTERVAL_SECONDS}s",
                    )
                    time.sleep(DAEMON_POLL_INTERVAL_SECONDS)
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
