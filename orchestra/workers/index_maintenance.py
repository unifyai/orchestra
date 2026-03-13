"""
Background worker for HNSW index maintenance.

This worker performs periodic maintenance on HNSW indexes to:
1. Check for and clean up invalid indexes (left by failed CONCURRENTLY operations)
2. Hard-delete soft-deleted embeddings in batches (to avoid long locks)
3. Use REINDEX CONCURRENTLY to rebuild indexes (keeps old index usable during rebuild)
4. Run VACUUM to reclaim disk space

The worker is triggered by Cloud Scheduler (short ops) or Cloud Run Jobs (reindex).

CRITICAL: REINDEX CONCURRENTLY must NOT run under HTTP timeout pressure.
An interrupted REINDEX corrupts both old and new indexes, leaving them invalid.
Always run reindex from a long-lived process (Cloud Run Job, manual invocation).

Recommended Production Setup:

Cloud Scheduler Jobs (short, time-bounded operations):

1. Health Check (twice daily) - Quick monitoring, no changes
   - Cron: 0 8,20 * * *
   - Endpoint: run_index_maintenance?mode=check
   - Attempt deadline: 2m, Max retries: 3

2. Cleanup (every 4 hours) - Frequent soft-delete cleanup
   - Cron: 0 */4 * * *
   - Endpoint: run_index_maintenance?mode=cleanup_only&skip_vacuum=true&max_duration=600
   - Attempt deadline: 15m, Max retries: 2

3. Cleanup + Vacuum (nightly) - Reclaim disk space
   - Cron: 0 3 * * *
   - Endpoint: run_index_maintenance?mode=cleanup_only&max_duration=1500
   - Attempt deadline: 30m, Max retries: 1

Cloud Run Job (weekly reindex - long-running, no HTTP timeout):

4. Full Reindex (weekly) - Optimize index structure
   - Cron trigger: 0 4 * * 0
   - Container: python -m orchestra.workers.index_maintenance
   - Env: MAINTENANCE_MODE=full, MAINTENANCE_SKIP_VACUUM=true
   - Task timeout: 3h, Max retries: 0

Usage (standalone):
    python -m orchestra.workers.index_maintenance

Environment Variables:
    DB_HOST, DB_USER, DB_PASS, DB_NAME: Database connection parameters
    INSTANCE_CONNECTION_NAME: Cloud SQL instance (for production)
    MAINTENANCE_MODE: auto|full|cleanup_only|reindex_only|check (default: auto)
    MAINTENANCE_SOFT_DELETE_THRESHOLD: int (default: 100)
    MAINTENANCE_SKIP_VACUUM: true|false (default: false)
    MAINTENANCE_MAX_DURATION: int seconds, 0=unlimited (default: 0)
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List, Literal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration Constants
# =============================================================================

# --- Batched Deletion ---
# Delete soft-deleted rows in batches to avoid long table locks
BATCH_DELETE_SIZE = 10000
MAX_DELETE_BATCHES = 1000  # Safety limit: max 10M rows per maintenance run

# --- Time Limits ---
# Default time budget for batched deletion (seconds). Jobs will stop early
# to leave time for other phases. Set via max_duration parameter.
DEFAULT_DELETION_TIME_BUDGET = 600  # 10 minutes default
# Safety margin before deadline to allow graceful completion (seconds)
DEADLINE_SAFETY_MARGIN = 60  # Stop 1 minute before deadline
# Minimum time required to safely start REINDEX CONCURRENTLY. If less time
# remains, reindex is skipped to avoid leaving invalid indexes behind.
# An interrupted REINDEX CONCURRENTLY corrupts both old and new indexes.
MIN_REINDEX_TIME_SECONDS = 1800  # 30 minutes

# --- Default Thresholds ---
# Minimum soft-deleted rows before triggering cleanup in 'auto' mode
DEFAULT_SOFT_DELETE_THRESHOLD = 100

# --- Index Definitions ---
# HNSW indexes on the embedding table
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

# Maintenance modes
MaintenanceMode = Literal["auto", "full", "cleanup_only", "reindex_only", "check"]

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
    """Get count of soft-deleted and orphaned embeddings pending cleanup."""
    result = conn.execute(
        text(
            "SELECT COUNT(*) FROM embedding WHERE is_deleted = true OR ref_id IS NULL",
        ),
    ).scalar()
    return result or 0


def get_total_embedding_count(conn) -> int:
    """Get total count of embeddings (for metrics)."""
    result = conn.execute(
        text("SELECT COUNT(*) FROM embedding"),
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


def get_invalid_indexes(conn) -> List[str]:
    """
    Find invalid indexes on the embedding table.

    Invalid indexes are left behind by failed CREATE INDEX CONCURRENTLY
    or REINDEX CONCURRENTLY operations.
    """
    try:
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
        return [row[0] for row in result.fetchall()]
    except Exception:
        return []


def cleanup_invalid_indexes(conn) -> List[str]:
    """
    Clean up invalid HNSW indexes.

    Returns:
        List of invalid index names that were cleaned up
    """
    invalid_indexes = get_invalid_indexes(conn)

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


def batched_delete_soft_deleted(
    conn,
    time_budget_seconds: int = DEFAULT_DELETION_TIME_BUDGET,
) -> dict:
    """
    Delete soft-deleted and orphaned embeddings in batches to avoid long locks.

    Cleans up embeddings that are:
    - Soft-deleted (is_deleted = true): Marked for deletion by application code
    - Orphaned (ref_id IS NULL): Parent LogEvent was deleted, FK set ref_id to NULL

    Args:
        conn: Database connection
        time_budget_seconds: Maximum time to spend on deletion. The function will
            stop early if approaching this limit to allow other phases to run.
            Set to 0 for unlimited (bounded only by MAX_DELETE_BATCHES).

    Returns:
        Dictionary with deletion metrics including whether it stopped early
    """
    start_time = time.time()
    total_deleted = 0
    batch_count = 0
    stopped_early = False
    stop_reason = None

    effective_budget = time_budget_seconds if time_budget_seconds > 0 else float("inf")

    logger.info(
        f"Starting batched deletion of soft-deleted/orphaned embeddings "
        f"(batch_size={BATCH_DELETE_SIZE}, time_budget={time_budget_seconds}s)",
    )

    while batch_count < MAX_DELETE_BATCHES:
        # Check shutdown flag
        if shutdown_flag:
            logger.info("Shutdown requested, stopping batched delete")
            stopped_early = True
            stop_reason = "shutdown_requested"
            break

        # Check time budget (with safety margin for batch completion)
        elapsed = time.time() - start_time
        if elapsed >= effective_budget - 30:  # 30s margin per batch
            logger.info(
                f"Time budget approaching ({elapsed:.1f}s / {effective_budget}s), "
                f"stopping to leave time for other phases",
            )
            stopped_early = True
            stop_reason = "time_budget_exceeded"
            break

        # Delete a batch using ctid for efficient row identification
        # Include both soft-deleted (is_deleted = true) AND orphaned (ref_id IS NULL)
        result = conn.execute(
            text(
                """
                WITH to_delete AS (
                    SELECT ctid FROM embedding
                    WHERE is_deleted = true OR ref_id IS NULL
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
            stop_reason = "all_deleted"
            break

        total_deleted += deleted_in_batch
        batch_count += 1

        if batch_count % 10 == 0:
            logger.info(
                f"Deleted {total_deleted} rows so far "
                f"({batch_count} batches, {time.time() - start_time:.1f}s elapsed)",
            )

    if batch_count >= MAX_DELETE_BATCHES:
        stopped_early = True
        stop_reason = "batch_limit_reached"

    duration = time.time() - start_time
    logger.info(
        f"Batched deletion complete: deleted={total_deleted}, "
        f"batches={batch_count}, duration={duration:.2f}s, "
        f"stopped_early={stopped_early}, reason={stop_reason}",
    )

    return {
        "total_deleted": total_deleted,
        "batch_count": batch_count,
        "duration": round(duration, 2),
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
    }


def reindex_hnsw_indexes(conn) -> dict:
    """
    Reindex HNSW indexes using REINDEX CONCURRENTLY.

    Unlike DROP/CREATE, REINDEX CONCURRENTLY:
    - Keeps the old index usable during the rebuild
    - Atomically swaps in the new index when ready
    - Only then removes the old index data

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
            if not check_index_exists(conn, index_name):
                logger.warning(f"Index {index_name} does not exist, creating it...")
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
                    "duration": round(time.time() - start, 2),
                    "success": True,
                }
            else:
                start = time.time()
                conn.execute(
                    text(f"REINDEX INDEX CONCURRENTLY {index_name}"),
                )
                results[index_name] = {
                    "action": "reindexed",
                    "duration": round(time.time() - start, 2),
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

            # Try to recreate if reindex failed
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


def run_vacuum(conn) -> float:
    """Run VACUUM on embedding table. Returns duration in seconds."""
    logger.info("Running VACUUM on embedding table...")
    start = time.time()
    conn.execute(text("VACUUM embedding"))
    duration = time.time() - start
    logger.info(f"VACUUM completed in {duration:.2f}s")
    return round(duration, 2)


def run_index_maintenance(
    session: Session,
    mode: MaintenanceMode = "auto",
    soft_delete_threshold: int = DEFAULT_SOFT_DELETE_THRESHOLD,
    skip_vacuum: bool = False,
    max_duration_seconds: int = 0,
) -> dict:
    """
    Perform HNSW index maintenance with configurable modes.

    Modes:
    - 'auto': Smart threshold-based (cleanup if >= threshold, reindex if cleanup happened)
    - 'full': Run all phases regardless of thresholds
    - 'cleanup_only': Only delete soft-deleted rows (no reindex)
    - 'reindex_only': Only reindex (no deletion)
    - 'check': Dry run - just report metrics without making changes

    Args:
        session: Database session
        mode: Maintenance mode
        soft_delete_threshold: Min soft-deleted rows for 'auto' mode cleanup
        skip_vacuum: Skip VACUUM phase (faster but doesn't reclaim disk)
        max_duration_seconds: Maximum total duration for this job. When set,
            the job will allocate time budgets to phases and stop gracefully
            before the deadline. Set to 0 for unlimited (default).
            Recommended values:
            - cleanup_only: 600-900 (10-15 min)
            - full: 3600-7200 (1-2 hours)
            - check: 60 (1 min)

    Returns:
        Dictionary with metrics from the maintenance operation
    """
    job_start_time = time.time()

    metrics = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "soft_delete_threshold": soft_delete_threshold,
        "max_duration_seconds": max_duration_seconds,
        "soft_deleted_count": 0,
        "total_embeddings": 0,
        "invalid_indexes_found": [],
        "invalid_indexes_cleaned": [],
        "deletion_metrics": {},
        "reindex_results": {},
        "index_sizes_before": {},
        "index_sizes_after": {},
        "durations": {},
        "phases_executed": [],
        "skipped": False,
        "stopped_early": False,
        "success": False,
        "error": None,
    }

    # Get a raw connection for DDL statements (CONCURRENTLY requires autocommit)
    conn = get_raw_connection(session)

    try:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        # Gather initial metrics
        metrics["soft_deleted_count"] = get_soft_deleted_count(conn)
        metrics["total_embeddings"] = get_total_embedding_count(conn)
        metrics["invalid_indexes_found"] = get_invalid_indexes(conn)

        for index_info in HNSW_INDEXES:
            index_name = index_info["name"]
            metrics["index_sizes_before"][index_name] = get_index_size(conn, index_name)

        logger.info(
            f"Index maintenance starting. Mode: {mode}, "
            f"Soft-deleted: {metrics['soft_deleted_count']}, "
            f"Invalid indexes: {len(metrics['invalid_indexes_found'])}",
        )

        # Handle 'check' mode - just return metrics
        if mode == "check":
            metrics["skipped"] = True
            metrics["success"] = True
            metrics["end_time"] = datetime.now(timezone.utc).isoformat()
            return metrics

        # Determine what work to do based on mode
        should_cleanup = mode in ("full", "cleanup_only") or (
            mode == "auto" and metrics["soft_deleted_count"] >= soft_delete_threshold
        )
        should_reindex = mode in ("full", "reindex_only")

        if mode == "auto" and should_cleanup:
            # In auto mode, only trigger reindex if we have enough time budget.
            # REINDEX CONCURRENTLY is dangerous under time pressure — an interrupted
            # reindex corrupts both old and new indexes, leaving them invalid.
            if (
                max_duration_seconds > 0
                and max_duration_seconds < MIN_REINDEX_TIME_SECONDS
            ):
                logger.info(
                    f"Auto mode: skipping reindex (max_duration={max_duration_seconds}s "
                    f"< minimum {MIN_REINDEX_TIME_SECONDS}s). "
                    f"Use mode=full via Cloud Run Job for safe reindexing.",
                )
                should_reindex = False
            elif max_duration_seconds == 0:
                should_reindex = True
            else:
                should_reindex = True

        # In 'auto' mode, skip if nothing to do
        if (
            mode == "auto"
            and not should_cleanup
            and not metrics["invalid_indexes_found"]
        ):
            metrics["skipped"] = True
            metrics["success"] = True
            metrics["end_time"] = datetime.now(timezone.utc).isoformat()
            logger.info(
                f"Skipping maintenance: {metrics['soft_deleted_count']} soft-deleted "
                f"(threshold: {soft_delete_threshold})",
            )
            return metrics

        # Phase 1: Always clean up invalid indexes (quick and important)
        logger.info("Phase 1: Cleaning up invalid indexes...")
        start = time.time()
        metrics["invalid_indexes_cleaned"] = cleanup_invalid_indexes(conn)
        metrics["durations"]["invalid_index_cleanup"] = round(time.time() - start, 2)
        metrics["phases_executed"].append("invalid_index_cleanup")

        # Phase 2: Batched deletion of soft-deleted embeddings
        if should_cleanup and metrics["soft_deleted_count"] > 0:
            # Calculate time budget for deletion phase
            if max_duration_seconds > 0:
                elapsed = time.time() - job_start_time
                remaining = max_duration_seconds - elapsed - DEADLINE_SAFETY_MARGIN
                # Reserve time for reindex (if applicable) and vacuum
                # Reindex can take 30+ min for large indexes, vacuum ~1 min
                if should_reindex:
                    # Leave most time for reindex, cap deletion at 20% of remaining
                    deletion_budget = min(remaining * 0.2, 600)  # Max 10 min
                else:
                    # No reindex, use most of remaining time
                    deletion_budget = remaining - 120  # Leave 2 min for vacuum
                deletion_budget = max(60, deletion_budget)  # At least 1 minute
            else:
                deletion_budget = DEFAULT_DELETION_TIME_BUDGET

            logger.info(
                f"Phase 2: Batched deletion of soft-deleted embeddings "
                f"(time_budget={deletion_budget:.0f}s)...",
            )
            start = time.time()
            metrics["deletion_metrics"] = batched_delete_soft_deleted(
                conn,
                time_budget_seconds=int(deletion_budget),
            )
            metrics["durations"]["batched_delete"] = round(time.time() - start, 2)
            metrics["phases_executed"].append("batched_delete")

            if metrics["deletion_metrics"].get("stopped_early"):
                metrics["stopped_early"] = True
        else:
            logger.info("Phase 2: Skipped (no cleanup needed or mode is reindex_only)")
            metrics["deletion_metrics"] = {"total_deleted": 0, "skipped": True}

        # Phase 3: Reindex HNSW indexes
        if should_reindex:
            # Check if we have enough time remaining for reindex.
            # REINDEX CONCURRENTLY is atomic and cannot be safely interrupted —
            # a killed operation leaves BOTH old and new indexes as invalid.
            if max_duration_seconds > 0:
                elapsed = time.time() - job_start_time
                remaining = max_duration_seconds - elapsed - DEADLINE_SAFETY_MARGIN
                if remaining < MIN_REINDEX_TIME_SECONDS:
                    logger.warning(
                        f"Skipping reindex: only {remaining:.0f}s remaining, "
                        f"need at least {MIN_REINDEX_TIME_SECONDS}s. "
                        f"Run mode=full via Cloud Run Job for safe reindexing.",
                    )
                    metrics["reindex_results"] = {
                        "skipped": True,
                        "reason": "insufficient_time",
                        "remaining_seconds": round(remaining, 0),
                        "min_required_seconds": MIN_REINDEX_TIME_SECONDS,
                    }
                    metrics["stopped_early"] = True
                else:
                    logger.info(
                        f"Phase 3: Reindexing HNSW indexes "
                        f"({remaining:.0f}s remaining)...",
                    )
                    start = time.time()
                    metrics["reindex_results"] = reindex_hnsw_indexes(conn)
                    metrics["durations"]["reindex"] = round(time.time() - start, 2)
                    metrics["phases_executed"].append("reindex")
            else:
                logger.info("Phase 3: Reindexing HNSW indexes...")
                start = time.time()
                metrics["reindex_results"] = reindex_hnsw_indexes(conn)
                metrics["durations"]["reindex"] = round(time.time() - start, 2)
                metrics["phases_executed"].append("reindex")
        else:
            logger.info("Phase 3: Skipped (mode is cleanup_only)")
            metrics["reindex_results"] = {"skipped": True}

        # Phase 4: VACUUM to reclaim space
        if not skip_vacuum and metrics["phases_executed"]:
            logger.info("Phase 4: Running VACUUM...")
            metrics["durations"]["vacuum"] = run_vacuum(conn)
            metrics["phases_executed"].append("vacuum")
        else:
            logger.info("Phase 4: Skipped (skip_vacuum=True or no work done)")

        # Gather final metrics
        for index_info in HNSW_INDEXES:
            index_name = index_info["name"]
            metrics["index_sizes_after"][index_name] = get_index_size(conn, index_name)

        metrics["success"] = True
        metrics["end_time"] = datetime.now(timezone.utc).isoformat()

        total_duration = sum(metrics["durations"].values())
        logger.info(f"Index maintenance completed in {total_duration:.2f}s")
        logger.info(f"Phases executed: {metrics['phases_executed']}")

    except Exception as e:
        logger.error(f"Index maintenance failed: {e}", exc_info=True)
        metrics["error"] = str(e)
        metrics["success"] = False
        metrics["end_time"] = datetime.now(timezone.utc).isoformat()

    finally:
        conn.close()

    return metrics


# Backward compatibility alias
def rebuild_hnsw_indexes(session: Session) -> dict:
    """Legacy function - use run_index_maintenance() instead."""
    return run_index_maintenance(session, mode="full")


def main():
    """
    Main entry point for the index maintenance worker.

    Designed to be invoked as a standalone process (e.g. Cloud Run Job).
    Configure via environment variables:

        MAINTENANCE_MODE: auto|full|cleanup_only|reindex_only|check (default: auto)
        MAINTENANCE_SOFT_DELETE_THRESHOLD: int (default: 100)
        MAINTENANCE_SKIP_VACUUM: true|false (default: false)
        MAINTENANCE_MAX_DURATION: int seconds, 0=unlimited (default: 0)

    Example Cloud Run Job configuration:
        Container image: your-registry/orchestra:latest
        Command: python -m orchestra.workers.index_maintenance
        Env vars:
            MAINTENANCE_MODE=full
            MAINTENANCE_SKIP_VACUUM=true
        Task timeout: 3h
        Max retries: 0
    """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    mode = os.environ.get("MAINTENANCE_MODE", "auto")
    soft_delete_threshold = int(
        os.environ.get("MAINTENANCE_SOFT_DELETE_THRESHOLD", "100"),
    )
    skip_vacuum = os.environ.get("MAINTENANCE_SKIP_VACUUM", "false").lower() == "true"
    max_duration = int(os.environ.get("MAINTENANCE_MAX_DURATION", "0"))

    logger.info(
        f"Index maintenance worker starting "
        f"(mode={mode}, threshold={soft_delete_threshold}, "
        f"skip_vacuum={skip_vacuum}, max_duration={max_duration}s)",
    )

    try:
        session = get_db_session()
        metrics = run_index_maintenance(
            session,
            mode=mode,
            soft_delete_threshold=soft_delete_threshold,
            skip_vacuum=skip_vacuum,
            max_duration_seconds=max_duration,
        )

        if metrics["success"]:
            deleted = metrics.get("deletion_metrics", {}).get("total_deleted", 0)
            logger.info(
                f"Maintenance completed: deleted={deleted}, "
                f"phases={metrics['phases_executed']}, "
                f"durations={metrics['durations']}",
            )
            sys.exit(0)
        else:
            logger.error(f"Maintenance failed: {metrics['error']}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Fatal error in maintenance: {e}", exc_info=True)
        sys.exit(1)

    finally:
        if "session" in locals():
            session.close()


if __name__ == "__main__":
    main()
