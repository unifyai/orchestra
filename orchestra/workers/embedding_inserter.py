"""
Stage 2: Embedding Index Insertion Worker (Serial)

This worker bulk inserts generated embedding vectors from the queue into
the indexed Embedding table.

Key characteristics:
- Should run SERIALLY (one worker at a time) for optimal HNSW performance
- Reads pre-generated vectors from embedding_queue.generated_vector
- Performs bulk INSERT into Embedding table
- Deletes successfully inserted items from queue

Flow:
1. Reset stale 'inserting' items back to 'vector_ready' (crash recovery)
2. Claim batch: vector_ready → inserting (atomic with FOR UPDATE SKIP LOCKED)
3. Bulk INSERT into Embedding table (ON CONFLICT DO UPDATE for soft-delete resurrection)
4. DELETE successfully inserted items from queue
5. On error: mark items as 'failed' with error message

Cloud Scheduler Configuration (1 serial job):
- Schedule: "*/3 * * * *" (every 3 minutes)
- URL: POST /admin/index_ready_embeddings?max_items=12000&max_time_seconds=150
- Attempt deadline: 180s

TODO: Migrate to Cloud Tasks for dynamic scaling. With Cloud Tasks,
a single task can handle larger batches with longer timeouts, and
tasks can be dispatched based on queue depth.
"""

import logging
import time
from typing import List, NamedTuple

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

# Safety multiplier for stale timeout calculation
# If max_time_seconds=150, stale timeout = 150 * 2 = 300 seconds (5 minutes)
STALE_TIMEOUT_SAFETY_MULTIPLIER = 2

# Minimum stale timeout in minutes (floor value regardless of calculation)
MIN_STALE_TIMEOUT_MINUTES = 3

# Default stale timeout when processing time is unknown
DEFAULT_STALE_TIMEOUT_MINUTES = 5

# Default processing bounds (can be overridden by API endpoint)
DEFAULT_MAX_ITEMS = 12000
DEFAULT_MAX_TIME_SECONDS = 150

# Chunk size for insertion as a fraction of max_items
# Ensures chunks scale with batch size while keeping commits reasonable
DEFAULT_CHUNK_FRACTION = 0.15  # 15% of max_items per chunk
MIN_CHUNK_SIZE = 500
MAX_CHUNK_SIZE = 5000


def calculate_stale_timeout_minutes(max_time_seconds: int) -> int:
    """
    Calculate appropriate stale timeout based on expected processing time.

    Args:
        max_time_seconds: Maximum expected processing time in seconds

    Returns:
        Stale timeout in minutes with safety margin
    """
    calculated_minutes = (max_time_seconds * STALE_TIMEOUT_SAFETY_MULTIPLIER) // 60
    return max(calculated_minutes, MIN_STALE_TIMEOUT_MINUTES)


def calculate_chunk_size(max_items: int) -> int:
    """
    Calculate appropriate chunk size based on batch size.

    Larger batches benefit from larger chunks (fewer commits),
    but we cap to avoid holding locks too long.

    Args:
        max_items: Maximum items being processed

    Returns:
        Chunk size for insertion batching
    """
    calculated = int(max_items * DEFAULT_CHUNK_FRACTION)
    return max(MIN_CHUNK_SIZE, min(calculated, MAX_CHUNK_SIZE))


class ReadyQueueItem(NamedTuple):
    """Represents a queue item with generated vector ready for insertion."""

    id: int
    ref_id: int
    key: str
    model: str
    generated_vector: list


# SQL query for atomic claiming with FOR UPDATE SKIP LOCKED
CLAIM_READY_QUERY = """
WITH claimable AS (
    SELECT id FROM embedding_queue
    WHERE status = 'vector_ready'
    ORDER BY created_at
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
UPDATE embedding_queue q
SET status = 'inserting',
    processing_started_at = NOW()
FROM claimable c
WHERE q.id = c.id
RETURNING q.id, q.ref_id, q.key, q.model, q.generated_vector
"""


def reset_stale_inserting_items(
    session: Session,
    stale_timeout_minutes: int = DEFAULT_STALE_TIMEOUT_MINUTES,
) -> int:
    """
    Reset queue items stuck in 'inserting' state for too long.

    This handles worker crashes by resetting items that have been
    in 'inserting' state for longer than the specified timeout.

    Uses FOR UPDATE SKIP LOCKED to avoid lock contention when multiple
    workers run concurrently.

    Args:
        session: Database session
        stale_timeout_minutes: Minutes after which 'inserting' items are considered stale.

    Returns:
        Number of items reset
    """
    result = session.execute(
        text(
            """
            UPDATE embedding_queue
            SET status = 'vector_ready',
                processing_started_at = NULL
            WHERE id IN (
                SELECT id FROM embedding_queue
                WHERE status = 'inserting'
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
            f"Reset {reset_count} stale items from 'inserting' back to 'vector_ready'",
        )
    return reset_count


def claim_ready_batch(session: Session, limit: int) -> List[ReadyQueueItem]:
    """
    Atomically claim a batch of vector_ready queue items.

    Uses FOR UPDATE SKIP LOCKED to allow safe parallel execution,
    though this endpoint should typically run serially.

    Args:
        session: Database session
        limit: Maximum number of items to claim

    Returns:
        List of claimed ReadyQueueItem objects
    """
    result = session.execute(
        text(CLAIM_READY_QUERY),
        {"limit": limit},
    )
    session.commit()

    rows = result.fetchall()
    items = []
    for row in rows:
        # Handle vector which may be returned as string or list
        vector = row[4]
        if isinstance(vector, str):
            # Parse if returned as string representation
            import json

            try:
                vector = json.loads(vector)
            except json.JSONDecodeError:
                # pgvector returns as '[x,y,z]' format
                vector = [float(x) for x in vector.strip("[]").split(",")]

        items.append(
            ReadyQueueItem(
                id=row[0],
                ref_id=row[1],
                key=row[2],
                model=row[3],
                generated_vector=vector,
            ),
        )
    return items


def bulk_insert_to_embedding_table(
    session: Session,
    items: List[ReadyQueueItem],
) -> int:
    """
    Perform bulk upsert of embeddings into the indexed Embedding table.

    Uses ON CONFLICT DO UPDATE to handle:
    - Duplicates (same ref_id, model, key)
    - Soft-deleted rows (resurrects them with new vector)

    Args:
        session: Database session
        items: List of ReadyQueueItem objects to insert

    Returns:
        Number of embeddings inserted/updated
    """
    from orchestra.db.models.orchestra_models import Embedding

    if not items:
        return 0

    # Prepare embedding objects for bulk insert
    embedding_dicts = [
        {
            "ref_id": item.ref_id,
            "key": item.key,
            "model": item.model,
            "vector": item.generated_vector,
            "is_deleted": False,
        }
        for item in items
    ]

    # Bulk upsert embeddings (handles duplicates and soft-deleted rows)
    stmt = insert(Embedding).values(embedding_dicts)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_embedding",
        set_={
            "vector": stmt.excluded.vector,
            "is_deleted": False,  # Resurrect soft-deleted embeddings
        },
    )
    result = session.execute(stmt)

    return len(items)


def delete_processed_queue_items(session: Session, item_ids: List[int]) -> int:
    """
    Delete successfully processed items from the queue.

    Args:
        session: Database session
        item_ids: List of queue item IDs to delete

    Returns:
        Number of items deleted
    """
    from orchestra.db.models.orchestra_models import EmbeddingQueue

    if not item_ids:
        return 0

    result = (
        session.query(EmbeddingQueue)
        .filter(
            EmbeddingQueue.id.in_(item_ids),
        )
        .delete(synchronize_session=False)
    )

    return result


def mark_insertion_failures(
    session: Session,
    item_ids: List[int],
    error_message: str,
) -> None:
    """
    Mark items as failed with error message.

    Args:
        session: Database session
        item_ids: List of queue item IDs that failed
        error_message: Error message to store
    """
    if not item_ids:
        return

    session.execute(
        text(
            """
            UPDATE embedding_queue
            SET status = 'failed',
                error_message = :error_msg,
                processing_started_at = NULL
            WHERE id = ANY(:ids)
        """,
        ),
        {"ids": item_ids, "error_msg": error_message[:500]},
    )


def get_insertion_queue_metrics(session: Session) -> dict:
    """Get current queue metrics for Stage 2 monitoring."""
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


def process_ready_embeddings(
    session: Session,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_time_seconds: int = DEFAULT_MAX_TIME_SECONDS,
    include_metrics: bool = False,
) -> dict:
    """
    Main entry point for Stage 2: Insert ready vectors into Embedding table.

    This function should run SERIALLY (one worker at a time) for optimal
    HNSW index performance. While technically safe with FOR UPDATE SKIP LOCKED,
    parallel insertion degrades performance due to index lock contention.

    Performance characteristics:
    - Bulk INSERT scales linearly with PostgreSQL/HNSW insertion rate
    - Chunk size auto-scales with max_items (15% per chunk, 500-5000 range)
    - Stale timeout auto-scales with max_time_seconds (2x safety margin)

    Args:
        session: Database session
        max_items: Maximum items to process in this invocation
        max_time_seconds: Maximum time to spend processing (also determines stale timeout)
        include_metrics: If True, include queue status counts (adds ~50ms overhead)

    Returns:
        Dictionary with processing metrics
    """
    start_time = time.time()

    # Calculate dynamic parameters based on inputs
    stale_timeout = calculate_stale_timeout_minutes(max_time_seconds)
    chunk_size = calculate_chunk_size(max_items)

    # Step 1: Reset any stale 'inserting' items (crash recovery)
    stale_reset = reset_stale_inserting_items(
        session,
        stale_timeout_minutes=stale_timeout,
    )

    # Step 2: Claim batch of vector_ready items
    claimed_items = claim_ready_batch(session, max_items)

    if not claimed_items:
        result = {
            "processed": 0,
            "inserted": 0,
            "failed": 0,
            "stale_reset": stale_reset,
            "duration_seconds": round(time.time() - start_time, 2),
            "queue_drained": True,
        }
        if include_metrics:
            result["queue_metrics"] = get_insertion_queue_metrics(session)
        return result

    # Step 3: Process in dynamically-sized chunks to avoid holding locks too long
    total_inserted = 0
    total_failed = 0
    successful_ids: List[int] = []
    failed_ids: List[int] = []

    for chunk_start in range(0, len(claimed_items), chunk_size):
        # Check time limit
        elapsed = time.time() - start_time
        if elapsed >= max_time_seconds:
            logger.warning(
                f"Time limit reached ({elapsed:.1f}s >= {max_time_seconds}s), "
                f"stopping after {total_inserted} insertions",
            )
            # Return unprocessed items to vector_ready
            remaining_ids = [item.id for item in claimed_items[chunk_start:]]
            if remaining_ids:
                session.execute(
                    text(
                        """
                        UPDATE embedding_queue
                        SET status = 'vector_ready',
                            processing_started_at = NULL
                        WHERE id = ANY(:ids)
                    """,
                    ),
                    {"ids": remaining_ids},
                )
                session.commit()
            break

        chunk = claimed_items[chunk_start : chunk_start + chunk_size]
        chunk_ids = [item.id for item in chunk]

        try:
            # Bulk insert chunk
            inserted = bulk_insert_to_embedding_table(session, chunk)
            total_inserted += inserted
            successful_ids.extend(chunk_ids)

            logger.info(
                f"Inserted chunk of {inserted} embeddings "
                f"({total_inserted}/{len(claimed_items)} total)",
            )

        except Exception as e:
            logger.error(f"Failed to insert chunk: {e}", exc_info=True)
            total_failed += len(chunk)
            failed_ids.extend(chunk_ids)

    # Step 5: Delete successfully processed items from queue
    if successful_ids:
        delete_processed_queue_items(session, successful_ids)

    # Step 6: Mark failed items
    if failed_ids:
        mark_insertion_failures(session, failed_ids, "Bulk insert failed")

    # Commit all changes
    session.commit()

    duration = round(time.time() - start_time, 2)

    result = {
        "processed": len(claimed_items),
        "inserted": total_inserted,
        "failed": total_failed,
        "stale_reset": stale_reset,
        "chunk_size_used": chunk_size,
        "duration_seconds": duration,
        "throughput_per_second": round(total_inserted / duration, 1)
        if duration > 0
        else 0,
    }

    # Only fetch metrics if explicitly requested (saves ~50ms per invocation)
    if include_metrics:
        result["queue_metrics"] = get_insertion_queue_metrics(session)

    return result
