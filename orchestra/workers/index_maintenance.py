"""
Background worker for HNSW index maintenance.

This worker performs periodic maintenance on HNSW indexes to:
1. Hard-delete soft-deleted embeddings (rows with is_deleted=true)
2. Rebuild HNSW indexes using CONCURRENTLY to avoid blocking
3. Run VACUUM to reclaim disk space

The worker is triggered by:
- Pub/Sub messages from project deletions (orchestra-embedding-maintenance topic)
- Cloud Scheduler for nightly maintenance (2 AM UTC)

Usage:
    python -m orchestra.workers.index_maintenance

Environment Variables:
    DB_HOST, DB_USER, DB_PASS, DB_NAME: Database connection parameters
    INSTANCE_CONNECTION_NAME: Cloud SQL instance (for production)

IMPORTANT: Uses DROP/CREATE INDEX CONCURRENTLY to avoid blocking production queries.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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


def rebuild_hnsw_indexes(session: Session) -> dict:
    """
    Rebuild HNSW indexes to clean up soft-deleted entries.

    This function:
    1. Drops existing HNSW indexes using CONCURRENTLY (non-blocking)
    2. Hard-deletes rows where is_deleted = true
    3. Recreates HNSW indexes with CONCURRENTLY
    4. Runs VACUUM to reclaim disk space

    Returns:
        Dictionary with metrics from the rebuild operation
    """
    metrics = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "deleted_count": 0,
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
        metrics["deleted_count"] = get_soft_deleted_count(conn)
        metrics["index_sizes_before"]["openai"] = get_index_size(
            conn,
            "embedding_hnsw_cosine_openai_1536_idx",
        )
        metrics["index_sizes_before"]["vertexai"] = get_index_size(
            conn,
            "embedding_hnsw_cosine_vertexai_1408_idx",
        )

        logger.info(
            f"Starting index maintenance. Soft-deleted rows: {metrics['deleted_count']}",
        )
        logger.info(f"Index sizes before: {metrics['index_sizes_before']}")

        if metrics["deleted_count"] == 0:
            logger.info("No soft-deleted embeddings to clean up. Skipping rebuild.")
            metrics["success"] = True
            return metrics

        # Phase 1: Drop existing HNSW indexes (CONCURRENTLY)
        logger.info("Phase 1: Dropping HNSW indexes...")
        start = time.time()

        conn.execute(
            text(
                "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_openai_1536_idx",
            ),
        )
        conn.execute(
            text(
                "DROP INDEX CONCURRENTLY IF EXISTS embedding_hnsw_cosine_vertexai_1408_idx",
            ),
        )

        metrics["durations"]["drop_indexes"] = time.time() - start
        logger.info(f"Indexes dropped in {metrics['durations']['drop_indexes']:.2f}s")

        # Phase 2: Hard-delete soft-deleted embeddings
        logger.info("Phase 2: Hard-deleting soft-deleted embeddings...")
        start = time.time()

        result = conn.execute(
            text("DELETE FROM embedding WHERE is_deleted = true"),
        )
        actual_deleted = result.rowcount

        metrics["durations"]["hard_delete"] = time.time() - start
        logger.info(
            f"Hard-deleted {actual_deleted} embeddings in {metrics['durations']['hard_delete']:.2f}s",
        )

        # Phase 3: Recreate HNSW indexes (CONCURRENTLY)
        logger.info("Phase 3: Creating HNSW indexes...")
        start = time.time()

        # OpenAI text-embedding-3-small (1536 dimensions)
        conn.execute(
            text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_openai_1536_idx
                ON embedding USING hnsw ((vector::vector(1536)) vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE model = 'text-embedding-3-small' AND is_deleted = false
            """,
            ),
        )

        # Vertex AI multimodalembedding@001 (1408 dimensions)
        conn.execute(
            text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_vertexai_1408_idx
                ON embedding USING hnsw ((vector::vector(1408)) vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE model = 'multimodalembedding@001' AND is_deleted = false
            """,
            ),
        )

        metrics["durations"]["create_indexes"] = time.time() - start
        logger.info(f"Indexes created in {metrics['durations']['create_indexes']:.2f}s")

        # Phase 4: VACUUM to reclaim space
        logger.info("Phase 4: Running VACUUM...")
        start = time.time()

        conn.execute(text("VACUUM (VERBOSE) embedding"))

        metrics["durations"]["vacuum"] = time.time() - start
        logger.info(f"VACUUM completed in {metrics['durations']['vacuum']:.2f}s")

        # Get final metrics
        metrics["index_sizes_after"]["openai"] = get_index_size(
            conn,
            "embedding_hnsw_cosine_openai_1536_idx",
        )
        metrics["index_sizes_after"]["vertexai"] = get_index_size(
            conn,
            "embedding_hnsw_cosine_vertexai_1408_idx",
        )

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

        # Try to recreate indexes if they were dropped but not recreated
        try:
            logger.info("Attempting to recreate indexes after failure...")
            conn.execute(
                text(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_openai_1536_idx
                    ON embedding USING hnsw ((vector::vector(1536)) vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
                    WHERE model = 'text-embedding-3-small' AND is_deleted = false
                """,
                ),
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS embedding_hnsw_cosine_vertexai_1408_idx
                    ON embedding USING hnsw ((vector::vector(1408)) vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
                    WHERE model = 'multimodalembedding@001' AND is_deleted = false
                """,
                ),
            )
            logger.info("Indexes recreated after failure")
        except Exception as recovery_error:
            logger.error(f"Failed to recreate indexes: {recovery_error}")

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
            logger.info(
                f"Maintenance completed: deleted={metrics['deleted_count']}, "
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
