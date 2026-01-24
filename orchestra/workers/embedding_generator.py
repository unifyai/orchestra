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

# Maximum batch size for a single OpenAI embedding API call
OPENAI_EMBEDDING_BATCH_SIZE = 2048

# Maximum retry attempts before marking item as 'failed'
MAX_RETRY_ATTEMPTS = 3

# Items stuck in 'generating' longer than this will be reset to 'pending'
STALE_GENERATING_TIMEOUT_MINUTES = 3

# Default processing bounds (can be overridden by API endpoint)
DEFAULT_MAX_ITEMS = 4096  # 2 OpenAI batches
DEFAULT_MAX_TIME_SECONDS = 40


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


def reset_stale_generating_items(session: Session) -> int:
    """
    Reset queue items stuck in 'generating' state for too long.

    This handles worker crashes by resetting items that have been
    in 'generating' state for longer than STALE_GENERATING_TIMEOUT_MINUTES.

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
            WHERE status = 'generating'
              AND processing_started_at IS NOT NULL
              AND processing_started_at < NOW() - INTERVAL ':minutes minutes'
        """.replace(
                ":minutes",
                str(STALE_GENERATING_TIMEOUT_MINUTES),
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
    Update queue items with generated vectors and set status to 'vector_ready'.

    Args:
        session: Database session
        successful_results: List of successful GenerationResult objects

    Returns:
        Number of items updated
    """
    if not successful_results:
        return 0

    try:
        for result in successful_results:
            session.execute(
                text(
                    """
                    UPDATE embedding_queue
                    SET status = 'vector_ready',
                        generated_vector = :vector,
                        vector_generated_at = NOW(),
                        processing_started_at = NULL
                    WHERE id = :queue_id
                """,
                ),
                {"queue_id": result.queue_item_id, "vector": result.vector},
            )

        session.commit()
        logger.info(f"Updated {len(successful_results)} items to 'vector_ready'")
        return len(successful_results)

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to update queue with vectors: {e}", exc_info=True)
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
    }


def process_pending_embeddings(
    session: Session,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_time_seconds: int = DEFAULT_MAX_TIME_SECONDS,
) -> dict:
    """
    Main entry point for Stage 1: Generate vectors for pending queue items.

    This function is SAFE FOR PARALLEL EXECUTION. Multiple workers can
    call this concurrently - FOR UPDATE SKIP LOCKED ensures each queue
    item is processed by exactly one worker.

    Args:
        session: Database session
        max_items: Maximum items to process in this invocation
        max_time_seconds: Maximum time to spend processing

    Returns:
        Dictionary with processing metrics
    """
    start_time = time.time()

    # Step 1: Reset any stale 'generating' items (crash recovery)
    stale_reset = reset_stale_generating_items(session)

    # Step 2: Get initial metrics
    initial_metrics = get_generation_queue_metrics(session)

    # Step 3: Claim batch of pending items
    claimed_items = claim_pending_batch(session, max_items)

    if not claimed_items:
        return {
            "processed": 0,
            "successful": 0,
            "failed": 0,
            "stale_reset": stale_reset,
            "queue_metrics": initial_metrics,
            "duration_seconds": round(time.time() - start_time, 2),
            "queue_drained": initial_metrics.get("pending", 0) == 0,
        }

    # Create lookup map for retry handling
    items_map = {item.id: item for item in claimed_items}

    # Step 4: Generate vectors (respecting time limit)
    # Note: OpenAI API calls are fast enough that we process full batch
    successful_results, failed_results = generate_vectors_for_items(claimed_items)

    # Step 5: Update queue with generated vectors
    updated_count = 0
    if successful_results:
        updated_count = update_queue_with_vectors(session, successful_results)

    # Step 6: Handle failures
    if failed_results:
        mark_generation_failures(session, failed_results, items_map)

    # Step 7: Get final metrics
    final_metrics = get_generation_queue_metrics(session)
    duration = round(time.time() - start_time, 2)

    return {
        "processed": len(claimed_items),
        "successful": len(successful_results),
        "failed": len(failed_results),
        "stale_reset": stale_reset,
        "queue_metrics": final_metrics,
        "duration_seconds": duration,
        "throughput_per_second": round(len(successful_results) / duration, 1)
        if duration > 0
        else 0,
        "queue_drained": final_metrics.get("pending", 0) == 0,
    }
