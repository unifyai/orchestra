"""
Background worker for HNSW index maintenance.

This worker performs periodic maintenance on HNSW indexes to:
1. Check for and clean up invalid indexes (left by failed CONCURRENTLY operations)
2. Hard-delete soft-deleted embeddings in batches (to avoid long locks)
3. Use REINDEX CONCURRENTLY to rebuild indexes (keeps old index usable during rebuild)
4. Run VACUUM to reclaim disk space

The worker is triggered by:
- Pub/Sub messages from project deletions (orchestra-embedding-maintenance topic)
- Cloud Scheduler for nightly maintenance (2 AM UTC)
- Manual API calls to /run_index_maintenance endpoint

Usage:
    python -m orchestra.workers.index_maintenance

Environment Variables:
    DB_HOST, DB_USER, DB_PASS, DB_NAME: Database connection parameters
    INSTANCE_CONNECTION_NAME: Cloud SQL instance (for production)

IMPORTANT: Uses REINDEX CONCURRENTLY instead of DROP/CREATE to avoid query degradation
during index rebuilds. The old index remains usable until the new one is ready.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
BATCH_DELETE_SIZE = 10000  # Delete soft-deleted rows in batches of 10k
MAX_DELETE_BATCHES = 1000  # Safety limit: max 10M rows per maintenance run

# Index definitions for the embedding table
HNSW_INDEXES = [
    {
        "name": "embedding_hnsw_cosine_openai_1536_idx",
        "model": "text-embedding-3-small",
        "dimensions": 1536,
    },
    {
        "name": "embedding_hnsw_cosine_vertexai_1408_idx",
        "model": "multimodalembedding@001",
        "dimensions": 1408,
    },
]

# Global shutdown flag
shutdown_flag = False


def signal_handler(signum, frame):
    """Handle graceful shutdown."""
    global shutdown_flag
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    shutdown_flag = True


def get_db_session() -> Session:
    """Create database session for worker."""
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


def get_raw_connection(session: Session):
    """Get raw database connection for executing DDL statements."""
    return session.get_bind().connect()


def get_index_size(conn, index_name: str) -> int:
    """Get size of an index in bytes."""
    try:
        result = conn.execute(
            text(f"SELECT pg_relation_size('{index_name}')"),
        ).scalar()
        return result or 0
    except Exception:
        return 0


def get_soft_deleted_count(conn) -> int:
    """Get count of soft-deleted embeddings."""
    result = conn.execute(
        text("SELECT COUNT(*) FROM embedding WHERE is_deleted = true"),
    ).scalar()
    return result or 0


def check_index_exists(conn, index_name: str) -> bool:
    """Check if an index exists."""
    result = conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = :index_name
            )
        """,
        ),
        {"index_name": index_name},
    ).scalar()
    return result or False


def check_and_cleanup_invalid_indexes(conn) -> List[str]:
    """
    Check for and clean up invalid HNSW indexes.

    Invalid indexes can be left behind by failed CREATE INDEX CONCURRENTLY
    or REINDEX CONCURRENTLY operations. They still incur write overhead
    and should be cleaned up.

    Returns:
        List of invalid index names that were cleaned up
    """
    # Find invalid indexes on the embedding table
    result = conn.execute(
        text(
            """
            SELECT i.indexname
            FROM pg_indexes i
            JOIN pg_class c ON c.relname = i.indexname
            JOIN pg_index idx ON idx.indexrelid = c.oid
            WHERE i.tablename = 'embedding'
              AND idx.indisvalid = false
        """,
        ),
    )
    invalid_indexes = [row[0] for row in result.fetchall()]

    if not invalid_indexes:
        logger.info("No invalid indexes found")
        return []

    logger.warning(f"Found {len(invalid_indexes)} invalid indexes: {invalid_indexes}")

    cleaned = []
    for index_name in invalid_indexes:
        try:
            logger.info(f"Dropping invalid index: {index_name}")
            conn.execute(
                text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"),
            )
            cleaned.append(index_name)
            logger.info(f"Successfully dropped invalid index: {index_name}")
        except Exception as e:
            logger.error(f"Failed to drop invalid index {index_name}: {e}")

    return cleaned


def batched_delete_soft_deleted(conn) -> dict:
    """
    Delete soft-deleted embeddings in batches to avoid long locks.

    Instead of a single DELETE that could lock the table for minutes,
    this deletes in batches of BATCH_DELETE_SIZE rows at a time.

    Returns:
        Dictionary with deletion metrics
    """
    start_time = time.time()
    total_deleted = 0
    batch_count = 0

    logger.info(
        f"Starting batched deletion of soft-deleted embeddings "
        f"(batch_size={BATCH_DELETE_SIZE})",
    )

    while batch_count < MAX_DELETE_BATCHES:
        # Check for shutdown
        if shutdown_flag:
            logger.info("Shutdown requested, stopping batched delete")
            break

        # Delete a batch using ctid for efficient row identification
        result = conn.execute(
            text(
                """
                WITH to_delete AS (
                    SELECT ctid FROM embedding
                    WHERE is_deleted = true
                    LIMIT :batch_size
                )
                DELETE FROM embedding
                WHERE ctid IN (SELECT ctid FROM to_delete)
            """,
            ),
            {"batch_size": BATCH_DELETE_SIZE},
        )

        deleted_in_batch = result.rowcount
        if deleted_in_batch == 0:
            break

        total_deleted += deleted_in_batch
        batch_count += 1

        if batch_count % 10 == 0:
            logger.info(
                f"Deleted {total_deleted} rows so far "
                f"({batch_count} batches, {time.time() - start_time:.1f}s elapsed)",
            )

    duration = time.time() - start_time
    logger.info(
        f"Batched deletion complete: deleted={total_deleted}, "
        f"batches={batch_count}, duration={duration:.2f}s",
    )

    return {
        "total_deleted": total_deleted,
        "batch_count": batch_count,
        "duration": duration,
    }


def reindex_hnsw_indexes(conn) -> dict:
    """
    Reindex HNSW indexes using REINDEX CONCURRENTLY.

    Unlike DROP/CREATE, REINDEX CONCURRENTLY:
    - Keeps the old index usable during the rebuild
    - Atomically swaps in the new index when ready
    - Only then removes the old index data

    This prevents query degradation during index maintenance.

    Returns:
        Dictionary with reindex metrics per index
    """
    results = {}

    for index_info in HNSW_INDEXES:
        index_name = index_info["name"]
        model = index_info["model"]
        dims = index_info["dimensions"]

        logger.info(f"Reindexing {index_name} for model {model}")

        try:
            # Check if index exists
            if not check_index_exists(conn, index_name):
                logger.warning(
                    f"Index {index_name} does not exist, creating it...",
                )
                # Create the index if it doesn't exist
                start = time.time()
                conn.execute(
                    text(
                        f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                        ON embedding USING hnsw ((vector::vector({dims})) vector_cosine_ops)
                        WITH (m = 16, ef_construction = 64)
                        WHERE model = '{model}' AND is_deleted = false
                    """,
                    ),
                )
                results[index_name] = {
                    "action": "created",
                    "duration": time.time() - start,
                    "success": True,
                }
            else:
                # Reindex existing index
                start = time.time()
                conn.execute(
                    text(f"REINDEX INDEX CONCURRENTLY {index_name}"),
                )
                results[index_name] = {
                    "action": "reindexed",
                    "duration": time.time() - start,
                    "success": True,
                }

            logger.info(
                f"Successfully {results[index_name]['action']} {index_name} "
                f"in {results[index_name]['duration']:.2f}s",
            )

        except Exception as e:
            logger.error(f"Failed to reindex {index_name}: {e}", exc_info=True)
            results[index_name] = {
                "action": "failed",
                "error": str(e),
                "success": False,
            }

            # Try to create index if reindex failed (might be corrupted)
            try:
                logger.info(f"Attempting to recreate {index_name} after failure...")
                conn.execute(
                    text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"),
                )
                conn.execute(
                    text(
                        f"""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
                        ON embedding USING hnsw ((vector::vector({dims})) vector_cosine_ops)
                        WITH (m = 16, ef_construction = 64)
                        WHERE model = '{model}' AND is_deleted = false
                    """,
                    ),
                )
                results[index_name]["recovery"] = "recreated"
                logger.info(f"Successfully recreated {index_name} after failure")
            except Exception as recovery_error:
                logger.error(
                    f"Failed to recreate {index_name}: {recovery_error}",
                    exc_info=True,
                )
                results[index_name]["recovery"] = f"failed: {recovery_error}"

    return results


def rebuild_hnsw_indexes(session: Session) -> dict:
    """
    Perform HNSW index maintenance with minimal query impact.

    This function:
    1. Checks for and cleans up any invalid indexes
    2. Deletes soft-deleted embeddings in batches (avoids long locks)
    3. Uses REINDEX CONCURRENTLY to rebuild indexes (no downtime)
    4. Runs VACUUM to reclaim disk space

    Returns:
        Dictionary with metrics from the rebuild operation
    """
    metrics = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "soft_deleted_count": 0,
        "invalid_indexes_cleaned": [],
        "deletion_metrics": {},
        "reindex_results": {},
        "index_sizes_before": {},
        "index_sizes_after": {},
        "durations": {},
        "success": False,
        "error": None,
    }

    # Get a raw connection for DDL statements
    # CONCURRENTLY operations require autocommit mode
    conn = get_raw_connection(session)

    try:
        # Set autocommit for CONCURRENTLY operations
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        # Get initial metrics
        metrics["soft_deleted_count"] = get_soft_deleted_count(conn)
        for index_info in HNSW_INDEXES:
            index_name = index_info["name"]
            metrics["index_sizes_before"][index_name] = get_index_size(conn, index_name)

        logger.info(
            f"Starting index maintenance. Soft-deleted rows: {metrics['soft_deleted_count']}",
        )
        logger.info(f"Index sizes before: {metrics['index_sizes_before']}")

        # Phase 1: Check and cleanup invalid indexes
        logger.info("Phase 1: Checking for invalid indexes...")
        start = time.time()
        metrics["invalid_indexes_cleaned"] = check_and_cleanup_invalid_indexes(conn)
        metrics["durations"]["invalid_index_cleanup"] = time.time() - start

        # Phase 2: Batched deletion of soft-deleted embeddings
        if metrics["soft_deleted_count"] > 0:
            logger.info("Phase 2: Batched deletion of soft-deleted embeddings...")
            start = time.time()
            metrics["deletion_metrics"] = batched_delete_soft_deleted(conn)
            metrics["durations"]["batched_delete"] = time.time() - start
        else:
            logger.info("Phase 2: No soft-deleted embeddings to clean up, skipping")
            metrics["deletion_metrics"] = {"total_deleted": 0, "batch_count": 0}
            metrics["durations"]["batched_delete"] = 0

        # Phase 3: Reindex HNSW indexes using REINDEX CONCURRENTLY
        logger.info("Phase 3: Reindexing HNSW indexes...")
        start = time.time()
        metrics["reindex_results"] = reindex_hnsw_indexes(conn)
        metrics["durations"]["reindex"] = time.time() - start

        # Phase 4: VACUUM to reclaim space
        logger.info("Phase 4: Running VACUUM...")
        start = time.time()
        conn.execute(text("VACUUM embedding"))
        metrics["durations"]["vacuum"] = time.time() - start
        logger.info(f"VACUUM completed in {metrics['durations']['vacuum']:.2f}s")

        # Get final metrics
        for index_info in HNSW_INDEXES:
            index_name = index_info["name"]
            metrics["index_sizes_after"][index_name] = get_index_size(conn, index_name)

        metrics["success"] = True
        metrics["end_time"] = datetime.now(timezone.utc).isoformat()

        total_duration = sum(metrics["durations"].values())
        logger.info(
            f"Index maintenance completed successfully in {total_duration:.2f}s",
        )
        logger.info(f"Index sizes after: {metrics['index_sizes_after']}")

    except Exception as e:
        logger.error(f"Index maintenance failed: {e}", exc_info=True)
        metrics["error"] = str(e)
        metrics["success"] = False
        metrics["end_time"] = datetime.now(timezone.utc).isoformat()

    finally:
        conn.close()

    return metrics


def run_maintenance() -> dict:
    """
    Run the index maintenance process.

    Returns:
        Dictionary with metrics from the maintenance run
    """
    logger.info("Starting index maintenance worker")

    try:
        session = get_db_session()
        metrics = rebuild_hnsw_indexes(session)

        # Log summary
        if metrics["success"]:
            deleted = metrics.get("deletion_metrics", {}).get("total_deleted", 0)
            logger.info(
                f"Maintenance completed: deleted={deleted}, "
                f"invalid_cleaned={len(metrics.get('invalid_indexes_cleaned', []))}, "
                f"durations={metrics['durations']}",
            )
        else:
            logger.error(f"Maintenance failed: {metrics['error']}")

        return metrics

    except Exception as e:
        logger.error(f"Fatal error in maintenance: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "start_time": datetime.now(timezone.utc).isoformat(),
        }

    finally:
        if "session" in locals():
            session.close()


def main():
    """Main entry point for the index maintenance worker."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Index maintenance worker starting")

    # Run maintenance once (Cloud Run Jobs are designed for single execution)
    metrics = run_maintenance()

    if metrics["success"]:
        logger.info("Index maintenance worker completed successfully")
        sys.exit(0)
    else:
        logger.error("Index maintenance worker failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
