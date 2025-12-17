"""
Background worker for processing embedding generation queue.

This worker:
1. Polls the embedding_queue table for pending items
2. Processes embeddings in batches (2048 - OpenAI recommended size)
3. Calls OpenAI API for embedding generation
4. Inserts embeddings into the database (with upsert for soft-deleted rows)
5. Handles retries for failed embeddings

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
from typing import List

from sqlalchemy import create_engine, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
BATCH_SIZE = 2048  # OpenAI recommended batch size
MAX_RETRIES = 3
PROCESSING_LIMIT = 10000  # Max items to process per run
POLL_INTERVAL = 30  # Seconds to wait when no work is available

# Global shutdown flag
shutdown_flag = False


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


def process_embedding_batch(session: Session, batch: List) -> None:
    """
    Process a batch of queued embeddings.

    Args:
        session: Database session
        batch: List of EmbeddingQueue items to process
    """
    # Import models here to avoid circular imports at module level
    from orchestra.db.models.orchestra_models import Embedding, EmbeddingQueue
    from orchestra.web.api.log.python2SQL.helpers import _get_embeddings_batch

    if not batch:
        return

    batch_ids = [item.id for item in batch]

    # Mark as processing
    session.execute(
        update(EmbeddingQueue)
        .where(EmbeddingQueue.id.in_(batch_ids))
        .values(status="processing"),
    )
    session.commit()

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


def process_pending_embeddings(session: Session, limit: int = PROCESSING_LIMIT) -> int:
    """
    Process pending embeddings in batches.

    Args:
        session: Database session
        limit: Maximum number of embeddings to process

    Returns:
        Number of embeddings processed
    """
    import time

    from orchestra.db.models.orchestra_models import EmbeddingQueue

    start_time = time.time()

    # Log queue metrics before processing
    queue_metrics = get_queue_metrics(session)
    logger.info(
        f"Queue metrics: pending={queue_metrics['pending']}, "
        f"processing={queue_metrics['processing']}, "
        f"failed={queue_metrics['failed']}",
    )

    # Fetch pending items
    pending = (
        session.query(EmbeddingQueue)
        .filter(
            EmbeddingQueue.status == "pending",
            EmbeddingQueue.retry_count < MAX_RETRIES,
        )
        .order_by(EmbeddingQueue.created_at)
        .limit(limit)
        .all()
    )

    if not pending:
        logger.debug("No pending embeddings to process")
        return 0

    logger.info(f"Found {len(pending)} pending embeddings to process")

    # Group by (model, dimensions) - can't batch different models together
    by_model = {}
    for item in pending:
        key = (item.model, item.dimensions)
        if key not in by_model:
            by_model[key] = []
        by_model[key].append(item)

    total_processed = 0
    total_errors = 0

    # Process each model's embeddings in batches
    for (model, dimensions), items in by_model.items():
        logger.info(f"Processing {len(items)} embeddings for model {model}")

        # Split into batches of BATCH_SIZE
        for i in range(0, len(items), BATCH_SIZE):
            if shutdown_flag:
                logger.info("Shutdown requested, stopping processing")
                break

            batch = items[i : i + BATCH_SIZE]
            try:
                process_embedding_batch(session, batch)
                total_processed += len(batch)
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                total_errors += len(batch)

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
        f"error_rate={error_rate:.2%}",
    )

    return total_processed


def run_once(session: Session) -> int:
    """
    Run one processing cycle.

    Args:
        session: Database session

    Returns:
        Number of embeddings processed
    """
    try:
        return process_pending_embeddings(session)
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
                processed = process_pending_embeddings(session)

                if processed == 0:
                    # No work to do, sleep for a bit
                    logger.debug(
                        f"No pending work, sleeping for {POLL_INTERVAL} seconds",
                    )
                    time.sleep(POLL_INTERVAL)
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
