"""
Stage 1: Embedding Generation Worker (Parallel-Safe)

This worker generates embedding vectors for pending queue items and stores
them back in the embedding_queue table with status='vector_ready'.

Key characteristics:
- SAFE FOR PARALLEL EXECUTION: Multiple workers can run concurrently
- Uses FOR UPDATE SKIP LOCKED to prevent race conditions
- Does NOT insert into the indexed Embedding table (that's Stage 2's job)
- Stores generated vectors in embedding_queue.generated_vector

Flow:
1. Reset stale 'generating' items back to 'pending' (crash recovery)
2. Claim batch: pending → generating (atomic with FOR UPDATE SKIP LOCKED)
3. Generate vectors via OpenAI API (batched by model)
4. Update queue: status='vector_ready', generated_vector=<vector>
5. On error: increment retry_count or mark 'failed'

Cloud Scheduler Configuration (2 parallel jobs):
- Job 1: Schedule "0,30 * * * * *" (at :00 and :30 of each minute)
- Job 2: Schedule "15,45 * * * * *" (at :15 and :45 of each minute)
- Both call: POST /admin/generate_pending_embeddings?max_items=4096

TODO: Migrate to Cloud Tasks for dynamic scaling based on queue depth.
Cloud Tasks can automatically scale workers based on demand rather than
fixed scheduling intervals.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy import text, update
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

# Maximum batch size for a single OpenAI embedding API call (OpenAI limit)
OPENAI_EMBEDDING_BATCH_SIZE = 2048

# Maximum retry attempts before marking item as 'failed'
MAX_RETRY_ATTEMPTS = 3

# Safety multiplier for stale timeout calculation
# If max_time_seconds=60, stale timeout = 60 * 3 = 180 seconds (3 minutes)
# This ensures items aren't reset while still being legitimately processed
STALE_TIMEOUT_SAFETY_MULTIPLIER = 3

# Minimum stale timeout in minutes (floor value regardless of calculation)
MIN_STALE_TIMEOUT_MINUTES = 5

# Default stale timeout when processing time is unknown (used by reset function)
DEFAULT_STALE_TIMEOUT_MINUTES = 10

# Default processing bounds (can be overridden by API endpoint)
DEFAULT_MAX_ITEMS = 4096  # 2 OpenAI batches
DEFAULT_MAX_TIME_SECONDS = 40


def calculate_stale_timeout_minutes(max_time_seconds: int) -> int:
    """
    Calculate appropriate stale timeout based on expected processing time.

    The stale timeout should be long enough to prevent resetting items that
    are legitimately being processed, but short enough to recover from crashes.

    Args:
        max_time_seconds: Maximum expected processing time in seconds

    Returns:
        Stale timeout in minutes with safety margin
    """
    # Calculate timeout with safety multiplier
    calculated_minutes = (max_time_seconds * STALE_TIMEOUT_SAFETY_MULTIPLIER) // 60

    # Ensure we meet the minimum threshold
    return max(calculated_minutes, MIN_STALE_TIMEOUT_MINUTES)


class PendingQueueItem(NamedTuple):
    """Represents a pending queue item to be processed by Stage 1."""

    id: int
    ref_id: int
    key: str
    text: str
    model: str
    dimensions: Optional[int]
    retry_count: int


@dataclass
class GenerationResult:
    """Result of a single embedding generation."""

    queue_item_id: int
    ref_id: int
    key: str
    model: str
    vector: list
    success: bool
    error_message: Optional[str] = None


# SQL query for atomic claiming with FOR UPDATE SKIP LOCKED
# This prevents race conditions when multiple workers run concurrently
CLAIM_PENDING_QUERY = """
WITH claimable AS (
    SELECT id FROM embedding_queue
    WHERE status = 'pending'
      AND retry_count < :max_retries
    ORDER BY created_at
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
UPDATE embedding_queue q
SET status = 'generating',
    processing_started_at = NOW()
FROM claimable c
WHERE q.id = c.id
RETURNING q.id, q.ref_id, q.key, q.text, q.model, q.dimensions, q.retry_count
"""

# SQL query for claiming FAILED items for retry
# Resets retry_count to 0 and sets status to 'generating'
CLAIM_FAILED_QUERY = """
WITH claimable AS (
    SELECT id FROM embedding_queue
    WHERE status = 'failed'
    ORDER BY created_at
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
UPDATE embedding_queue q
SET status = 'generating',
    retry_count = 0,
    error_message = NULL,
    processing_started_at = NOW()
FROM claimable c
WHERE q.id = c.id
RETURNING q.id, q.ref_id, q.key, q.text, q.model, q.dimensions, q.retry_count
"""


def reset_stale_generating_items(
    session: Session,
    stale_timeout_minutes: int = DEFAULT_STALE_TIMEOUT_MINUTES,
) -> int:
    """
    Reset queue items stuck in 'generating' state for too long.

    This handles worker crashes by resetting items that have been
    in 'generating' state for longer than the specified timeout.

    Uses FOR UPDATE SKIP LOCKED to avoid lock contention when multiple
    workers run concurrently.

    Args:
        session: Database session
        stale_timeout_minutes: Minutes after which 'generating' items are considered stale.
                              Should be set based on expected processing time with safety margin.

    Returns:
        Number of items reset
    """
    result = session.execute(
        text(
            """
            UPDATE embedding_queue
            SET status = 'pending',
                processing_started_at = NULL
            WHERE id IN (
                SELECT id FROM embedding_queue
                WHERE status = 'generating'
                  AND processing_started_at IS NOT NULL
                  AND processing_started_at < NOW() - INTERVAL ':minutes minutes'
                FOR UPDATE SKIP LOCKED
            )
        """.replace(
                ":minutes",
                str(stale_timeout_minutes),
            ),
        ),
    )
    session.commit()
    reset_count = result.rowcount
    if reset_count > 0:
        logger.warning(
            f"Reset {reset_count} stale items from 'generating' back to 'pending'",
        )
    return reset_count


def claim_pending_batch(session: Session, limit: int) -> List[PendingQueueItem]:
    """
    Atomically claim a batch of pending queue items.

    Uses FOR UPDATE SKIP LOCKED to prevent race conditions when
    multiple workers are running concurrently.

    Args:
        session: Database session
        limit: Maximum number of items to claim

    Returns:
        List of claimed PendingQueueItem objects
    """
    result = session.execute(
        text(CLAIM_PENDING_QUERY),
        {"max_retries": MAX_RETRY_ATTEMPTS, "limit": limit},
    )
    session.commit()

    rows = result.fetchall()
    return [
        PendingQueueItem(
            id=row[0],
            ref_id=row[1],
            key=row[2],
            text=row[3],
            model=row[4],
            dimensions=row[5],
            retry_count=row[6],
        )
        for row in rows
    ]


def claim_failed_batch(session: Session, limit: int) -> List[PendingQueueItem]:
    """
    Atomically claim a batch of FAILED queue items for retry.

    Resets retry_count to 0 and clears error_message, giving items
    a fresh set of retry attempts.

    Uses FOR UPDATE SKIP LOCKED to prevent race conditions when
    multiple workers are running concurrently.

    Args:
        session: Database session
        limit: Maximum number of failed items to retry

    Returns:
        List of claimed PendingQueueItem objects (retry_count will be 0)
    """
    result = session.execute(
        text(CLAIM_FAILED_QUERY),
        {"limit": limit},
    )
    session.commit()

    rows = result.fetchall()
    return [
        PendingQueueItem(
            id=row[0],
            ref_id=row[1],
            key=row[2],
            text=row[3],
            model=row[4],
            dimensions=row[5],
            retry_count=row[6],
        )
        for row in rows
    ]


def generate_vectors_for_items(
    items: List[PendingQueueItem],
) -> Tuple[List[GenerationResult], List[GenerationResult]]:
    """
    Generate embedding vectors for a list of queue items using OpenAI API.

    This function batches items by (model, dimensions) and calls the OpenAI API
    in chunks of OPENAI_EMBEDDING_BATCH_SIZE (2048).

    Args:
        items: List of PendingQueueItem objects to generate embeddings for

    Returns:
        Tuple of (successful_results, failed_results)
    """
    from orchestra.web.api.log.python2SQL.helpers import _get_embeddings_batch

    if not items:
        return [], []

    # Group by (model, dimensions) - can't batch different models together
    by_model: Dict[Tuple[str, Optional[int]], List[PendingQueueItem]] = defaultdict(
        list,
    )
    for item in items:
        key = (item.model, item.dimensions)
        by_model[key].append(item)

    successful_results: List[GenerationResult] = []
    failed_results: List[GenerationResult] = []

    for (model, dimensions), model_items in by_model.items():
        logger.info(
            f"Generating vectors for {len(model_items)} items with model {model}",
        )

        # Process in OpenAI-compatible batches (2048 max)
        for batch_start in range(0, len(model_items), OPENAI_EMBEDDING_BATCH_SIZE):
            batch = model_items[batch_start : batch_start + OPENAI_EMBEDDING_BATCH_SIZE]
            texts = [item.text for item in batch]

            try:
                # Call OpenAI API
                embeddings = _get_embeddings_batch(texts, model, dimensions)

                # Create successful results
                for i, item in enumerate(batch):
                    successful_results.append(
                        GenerationResult(
                            queue_item_id=item.id,
                            ref_id=item.ref_id,
                            key=item.key,
                            model=item.model,
                            vector=embeddings[i],
                            success=True,
                        ),
                    )

                logger.info(f"Generated {len(batch)} vectors for model {model}")

            except Exception as e:
                error_msg = str(e)[:500]
                logger.error(
                    f"Failed to generate vectors for batch: {e}",
                    exc_info=True,
                )
                # Mark entire batch as failed
                for item in batch:
                    failed_results.append(
                        GenerationResult(
                            queue_item_id=item.id,
                            ref_id=item.ref_id,
                            key=item.key,
                            model=item.model,
                            vector=[],
                            success=False,
                            error_message=error_msg,
                        ),
                    )

    return successful_results, failed_results


def update_queue_with_vectors(
    session: Session,
    successful_results: List[GenerationResult],
) -> int:
    """
    Update queue items with generated vectors using BULK UPDATE.

    Uses PostgreSQL's unnest() for single-statement bulk updates instead of
    N individual UPDATE statements. This reduces database round trips from
    O(N) to O(1), dramatically improving performance regardless of batch size.

    Performance scales linearly with PostgreSQL's ability to handle the UPDATE,
    not with the number of items (typically ~50-100ms for any reasonable batch).

    Args:
        session: Database session
        successful_results: List of successful GenerationResult objects

    Returns:
        Number of items updated
    """
    if not successful_results:
        return 0

    try:
        # Extract data for bulk update
        ids = [r.queue_item_id for r in successful_results]
        # Convert vectors to pgvector string format: [0.1, 0.2, ...] -> '[0.1, 0.2, ...]'
        # This format is required for CAST to vector[] to work correctly
        vector_strings = [str(v) for v in [r.vector for r in successful_results]]

        # Single bulk UPDATE using unnest - O(1) instead of O(N)
        # Only updates rows still in 'generating' status (prevents race with stale reset)
        # NOTE: Using CAST() instead of :: to avoid SQLAlchemy parameter binding conflict
        result = session.execute(
            text(
                """
                WITH update_data AS (
                    SELECT
                        unnest(CAST(:ids AS int[])) as id,
                        unnest(CAST(:vectors AS vector[])) as vector
                )
                UPDATE embedding_queue q
                SET status = 'vector_ready',
                    generated_vector = ud.vector,
                    vector_generated_at = NOW(),
                    processing_started_at = NULL
                FROM update_data ud
                WHERE q.id = ud.id
                  AND q.status = 'generating'
            """,
            ),
            {"ids": ids, "vectors": vector_strings},
        )

        # Commit the bulk update
        session.commit()

        updated_count = result.rowcount
        skipped = len(successful_results) - updated_count

        if skipped > 0:
            logger.warning(
                f"Skipped {skipped} items (status changed by another worker)",
            )
        logger.info(f"Bulk updated {updated_count} items to 'vector_ready'")
        return updated_count

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to bulk update queue with vectors: {e}", exc_info=True)
        raise


def mark_generation_failures(
    session: Session,
    failed_results: List[GenerationResult],
    items_map: Dict[int, PendingQueueItem],
) -> None:
    """
    Update failed items with incremented retry count.

    Items that exceed MAX_RETRY_ATTEMPTS are marked as 'failed'.
    Otherwise, they're returned to 'pending' for retry.

    Args:
        session: Database session
        failed_results: List of failed GenerationResult objects
        items_map: Mapping of queue_item_id to original PendingQueueItem
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    for result in failed_results:
        original_item = items_map.get(result.queue_item_id)
        if not original_item:
            continue

        new_retry_count = original_item.retry_count + 1
        new_status = "failed" if new_retry_count >= MAX_RETRY_ATTEMPTS else "pending"

        session.execute(
            update(EmbeddingQueue)
            .where(EmbeddingQueue.id == result.queue_item_id)
            .values(
                status=new_status,
                retry_count=new_retry_count,
                error_message=result.error_message,
                processing_started_at=None,
            ),
        )
    session.commit()


def get_generation_queue_metrics(session: Session) -> dict:
    """Get current queue metrics for Stage 1 monitoring."""
    from sqlalchemy import func

    from orchestra.db.models.orchestra_models import EmbeddingQueue

    # Count by status
    status_counts = dict(
        session.query(EmbeddingQueue.status, func.count(EmbeddingQueue.id))
        .group_by(EmbeddingQueue.status)
        .all(),
    )

    return {
        "pending": status_counts.get("pending", 0),
        "generating": status_counts.get("generating", 0),
        "vector_ready": status_counts.get("vector_ready", 0),
        "inserting": status_counts.get("inserting", 0),
        "failed": status_counts.get("failed", 0),
        "cancelled": status_counts.get("cancelled", 0),
    }


def process_pending_embeddings(
    session: Session,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_time_seconds: int = DEFAULT_MAX_TIME_SECONDS,
    include_metrics: bool = False,
    retry_failed: bool = False,
) -> dict:
    """
    Main entry point for Stage 1: Generate vectors for pending queue items.

    This function is SAFE FOR PARALLEL EXECUTION. Multiple workers can
    call this concurrently - FOR UPDATE SKIP LOCKED ensures each queue
    item is processed by exactly one worker.

    Performance characteristics:
    - OpenAI API: ~2-5 seconds per 2048 items (network bound)
    - Bulk UPDATE: ~50-100ms regardless of batch size (single SQL statement)
    - Total time scales primarily with OpenAI API calls, not DB operations

    Args:
        session: Database session
        max_items: Maximum items to process in this invocation
        max_time_seconds: Maximum time to spend processing (also determines stale timeout)
        include_metrics: If True, include queue status counts (adds ~50ms overhead)
        retry_failed: If True, retry items with status='failed' instead of 'pending'.
                     Failed items get their retry_count reset to 0 and error_message cleared.

    Returns:
        Dictionary with processing metrics
    """
    start_time = time.time()
    mode = "retry_failed" if retry_failed else "pending"

    # Calculate stale timeout based on expected processing time
    # This ensures items aren't reset while legitimately being processed
    stale_timeout = calculate_stale_timeout_minutes(max_time_seconds)

    # Step 1: Reset any stale 'generating' items (crash recovery)
    # Skip for retry_failed mode since those items are already in a terminal state
    stale_reset = 0
    if not retry_failed:
        stale_reset = reset_stale_generating_items(
            session,
            stale_timeout_minutes=stale_timeout,
        )

    # Step 2: Claim batch of items (atomic with FOR UPDATE SKIP LOCKED)
    if retry_failed:
        claimed_items = claim_failed_batch(session, max_items)
    else:
        claimed_items = claim_pending_batch(session, max_items)

    if not claimed_items:
        result = {
            "processed": 0,
            "successful": 0,
            "failed": 0,
            "stale_reset": stale_reset,
            "mode": mode,
            "duration_seconds": round(time.time() - start_time, 2),
            "queue_drained": True,  # No items to claim means queue is empty for us
        }
        if include_metrics:
            result["queue_metrics"] = get_generation_queue_metrics(session)
        return result

    # Create lookup map for retry handling
    items_map = {item.id: item for item in claimed_items}

    # Step 3: Generate vectors via OpenAI API
    # Time scales with number of OpenAI batches: ~2-5s per 2048 items
    successful_results, failed_results = generate_vectors_for_items(claimed_items)

    # Step 4: Bulk update queue with generated vectors (single statement, ~50ms)
    updated_count = 0
    if successful_results:
        updated_count = update_queue_with_vectors(session, successful_results)

    # Step 5: Handle failures
    if failed_results:
        mark_generation_failures(session, failed_results, items_map)

    duration = round(time.time() - start_time, 2)

    result = {
        "processed": len(claimed_items),
        "successful": len(successful_results),
        "updated": updated_count,
        "failed": len(failed_results),
        "stale_reset": stale_reset,
        "mode": mode,
        "duration_seconds": duration,
        "throughput_per_second": (
            round(len(successful_results) / duration, 1) if duration > 0 else 0
        ),
    }

    # Only fetch metrics if explicitly requested (saves ~50ms per invocation)
    if include_metrics:
        result["queue_metrics"] = get_generation_queue_metrics(session)

    return result
