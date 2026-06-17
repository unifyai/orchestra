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

from orchestra.db.partitioning import (
    find_promotion_candidates,
    is_partitioned,
    promote_project_to_partition,
    table_storage_units,
)

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
    """Get size of an index in bytes.

    For a partitioned index ``pg_relation_size`` of the parent is 0 (no storage),
    so sum the child index segments; falls back to the direct size for a plain
    index.
    """
    try:
        result = conn.execute(
            text(
                """
                SELECT COALESCE(
                    (SELECT SUM(pg_relation_size(inhrelid))
                     FROM pg_inherits WHERE inhparent = to_regclass(:idx)),
                    pg_relation_size(to_regclass(:idx)),
                    0
                )
                """,
            ),
            {"idx": index_name},
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


def embedding_leaf_tables(conn) -> List[str]:
    """Physical tables holding embedding rows.

    Child partitions when ``embedding`` is partitioned, else ``['embedding']``.
    Maintenance (cleanup/VACUUM/REINDEX) must operate on these leaf relations:
    ``ctid`` is per-physical-table and ``REINDEX CONCURRENTLY`` is not supported
    on a partitioned parent.
    """
    return table_storage_units(conn, "embedding")


def hnsw_leaf_indexes(conn) -> List[tuple]:
    """Return ``(index_name, table_name)`` for every leaf HNSW index on embedding.

    Restricted to the ``embedding`` family so other HNSW indexes (e.g. artifact
    embeddings) are left untouched. Works for both the partitioned layout (one
    index per partition) and the legacy single-table layout.
    """
    leaves = embedding_leaf_tables(conn)
    if not leaves:
        return []
    rows = conn.execute(
        text(
            """
            SELECT c.relname AS index_name, t.relname AS table_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_am am ON am.oid = c.relam
            WHERE am.amname = 'hnsw'
              AND c.relkind = 'i'
              AND t.relkind = 'r'
              AND t.relname = ANY(:leaves)
            ORDER BY t.relname, c.relname
            """,
        ),
        {"leaves": leaves},
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


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
        leaves = embedding_leaf_tables(conn)
        if not leaves:
            return []
        result = conn.execute(
            text(
                """
                SELECT c.relname
                FROM pg_index idx
                JOIN pg_class c ON c.oid = idx.indexrelid
                JOIN pg_class t ON t.oid = idx.indrelid
                WHERE t.relname = ANY(:leaves)
                  AND c.relkind = 'i'
                  AND idx.indisvalid = false
            """,
            ),
            {"leaves": leaves},
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

    # ctid is per physical table, so iterate the leaf partitions (one relation
    # for a non-partitioned embedding table) rather than the partitioned parent.
    leaves = embedding_leaf_tables(conn)

    logger.info(
        f"Starting batched deletion of soft-deleted/orphaned embeddings across "
        f"{len(leaves)} partition(s) "
        f"(batch_size={BATCH_DELETE_SIZE}, time_budget={time_budget_seconds}s)",
    )

    for leaf in leaves:
        if stopped_early:
            break
        while batch_count < MAX_DELETE_BATCHES:
            if shutdown_flag:
                logger.info("Shutdown requested, stopping batched delete")
                stopped_early = True
                stop_reason = "shutdown_requested"
                break

            elapsed = time.time() - start_time
            if elapsed >= effective_budget - 30:  # 30s margin per batch
                logger.info(
                    f"Time budget approaching ({elapsed:.1f}s / {effective_budget}s), "
                    f"stopping to leave time for other phases",
                )
                stopped_early = True
                stop_reason = "time_budget_exceeded"
                break

            # Delete a batch using ctid (valid within this single leaf relation).
            # Includes soft-deleted (is_deleted = true) AND orphaned (ref_id NULL).
            result = conn.execute(
                text(
                    f"""
                    WITH to_delete AS (
                        SELECT ctid FROM "{leaf}"
                        WHERE is_deleted = true OR ref_id IS NULL
                        LIMIT :batch_size
                    )
                    DELETE FROM "{leaf}"
                    WHERE ctid IN (SELECT ctid FROM to_delete)
                """,
                ),
                {"batch_size": BATCH_DELETE_SIZE},
            )

            deleted_in_batch = result.rowcount
            if deleted_in_batch == 0:
                break  # this leaf is drained; move to the next

            total_deleted += deleted_in_batch
            batch_count += 1

            if batch_count % 10 == 0:
                logger.info(
                    f"Deleted {total_deleted} rows so far "
                    f"({batch_count} batches, {time.time() - start_time:.1f}s elapsed)",
                )

    if not stopped_early and stop_reason is None:
        stop_reason = "all_deleted"

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


def _ensure_legacy_hnsw_indexes(conn) -> None:
    """Self-heal the HNSW indexes on a non-partitioned (legacy) embedding table.

    On the partitioned layout the per-partition indexes are owned by the parent
    partitioned index (created by the migration/runbook/ATTACH), so the worker
    only reindexes them; it does not create them. This create-if-missing path is
    only for the legacy single-table layout.
    """
    if is_partitioned(conn, "embedding"):
        return
    for index_info in HNSW_INDEXES:
        if check_index_exists(conn, index_info["name"]):
            continue
        logger.warning(
            f"Index {index_info['name']} missing on legacy embedding table, "
            f"creating it...",
        )
        conn.execute(
            text(
                f"""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_info['name']}
                ON embedding USING hnsw
                    ((vector::vector({index_info['dimensions']})) vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE model = '{index_info['model']}' AND is_deleted = false
            """,
            ),
        )


def reindex_hnsw_indexes(conn) -> dict:
    """
    Reindex every leaf HNSW index using REINDEX INDEX CONCURRENTLY.

    REINDEX CONCURRENTLY is not supported on a partitioned (parent) index, so we
    rebuild each leaf-partition index individually. REINDEX CONCURRENTLY keeps
    the old index usable during the rebuild and atomically swaps the new one in.
    A failed leaf is left for the next run / invalid-index cleanup rather than
    risking a non-concurrent recreate that would lock the partition.

    Returns:
        Dictionary with reindex metrics per leaf index
    """
    results: dict = {}

    _ensure_legacy_hnsw_indexes(conn)

    leaf_indexes = hnsw_leaf_indexes(conn)
    logger.info(f"Reindexing {len(leaf_indexes)} leaf HNSW index(es)")

    for index_name, table_name in leaf_indexes:
        logger.info(f"Reindexing {index_name} (partition {table_name})")
        try:
            start = time.time()
            conn.execute(text(f'REINDEX INDEX CONCURRENTLY "{index_name}"'))
            results[index_name] = {
                "action": "reindexed",
                "table": table_name,
                "duration": round(time.time() - start, 2),
                "success": True,
            }
            logger.info(
                f"Successfully reindexed {index_name} "
                f"in {results[index_name]['duration']:.2f}s",
            )
        except Exception as e:
            logger.error(f"Failed to reindex {index_name}: {e}", exc_info=True)
            results[index_name] = {
                "action": "failed",
                "table": table_name,
                "error": str(e),
                "success": False,
            }

    return results


def run_vacuum(conn) -> float:
    """VACUUM the embedding partitions individually. Returns total seconds.

    Per-partition VACUUM keeps a single giant partition from blocking the rest
    and lets dead tuples from per-partition cleanup be reclaimed independently.
    """
    leaves = embedding_leaf_tables(conn)
    logger.info(f"Running VACUUM on {len(leaves)} embedding partition(s)...")
    start = time.time()
    for leaf in leaves:
        conn.execute(text(f'VACUUM "{leaf}"'))
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


# Minimum log_event rows for a project in DEFAULT to earn its own partition.
DEFAULT_PROMOTION_THRESHOLD = 500_000


def run_partition_provisioning(
    session: Session,
    threshold: int = DEFAULT_PROMOTION_THRESHOLD,
    max_promotions: int = 1,
    dry_run: bool = False,
) -> dict:
    """Promote large DEFAULT-partition projects into dedicated partitions.

    A project is promoted by moving its rows out of the DEFAULT partition into a
    new dedicated partition (which then carries its own GIN/HNSW indexes), so a
    later deletion of that project becomes an O(1) ``DROP PARTITION``. The move
    is per-row, so promotion runs proactively at a threshold rather than once a
    project is already enormous. ``max_promotions`` bounds the work per run since
    each promotion (especially the HNSW build on ATTACH) is expensive.
    """
    metrics: dict = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "threshold": threshold,
        "max_promotions": max_promotions,
        "dry_run": dry_run,
        "candidates": [],
        "promoted": {},
        "success": False,
        "error": None,
    }
    try:
        with session.get_bind().connect() as probe:
            if not is_partitioned(probe, "log_event"):
                logger.info(
                    "log_event is not partitioned; skipping partition provisioning",
                )
                metrics["success"] = True
                metrics["skipped"] = True
                return metrics
            candidates = find_promotion_candidates(probe, threshold)

        metrics["candidates"] = candidates
        logger.info(
            f"Partition provisioning: {len(candidates)} project(s) over "
            f"threshold {threshold}; promoting up to {max_promotions}",
        )

        for project_id, row_count in candidates[:max_promotions]:
            if shutdown_flag:
                break
            if dry_run:
                logger.info(
                    f"[dry-run] would promote project {project_id} ({row_count} rows)",
                )
                metrics["promoted"][project_id] = {
                    "row_count": row_count,
                    "dry_run": True,
                }
                continue
            logger.info(f"Promoting project {project_id} ({row_count} rows)...")
            start = time.time()
            conn = session.get_bind().connect()
            try:
                with conn.begin():
                    created = promote_project_to_partition(conn, project_id)
                metrics["promoted"][project_id] = {
                    "row_count": row_count,
                    "partitions": created,
                    "duration": round(time.time() - start, 2),
                }
                logger.info(
                    f"Promoted project {project_id} in "
                    f"{metrics['promoted'][project_id]['duration']:.1f}s: {created}",
                )
            finally:
                conn.close()

        metrics["success"] = True
    except Exception as e:
        logger.error(f"Partition provisioning failed: {e}", exc_info=True)
        metrics["error"] = str(e)
        metrics["success"] = False
    metrics["end_time"] = datetime.now(timezone.utc).isoformat()
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

        # Partition provisioning runs as its own mode (promotes large DEFAULT
        # projects into dedicated partitions); it is orthogonal to HNSW upkeep.
        if mode == "promote":
            threshold = int(
                os.environ.get(
                    "MAINTENANCE_PROMOTION_THRESHOLD",
                    str(DEFAULT_PROMOTION_THRESHOLD),
                ),
            )
            max_promotions = int(os.environ.get("MAINTENANCE_MAX_PROMOTIONS", "1"))
            dry_run = os.environ.get("MAINTENANCE_DRY_RUN", "false").lower() == "true"
            metrics = run_partition_provisioning(
                session,
                threshold=threshold,
                max_promotions=max_promotions,
                dry_run=dry_run,
            )
            if metrics["success"]:
                logger.info(f"Provisioning completed: promoted={metrics['promoted']}")
                sys.exit(0)
            logger.error(f"Provisioning failed: {metrics['error']}")
            sys.exit(1)

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
