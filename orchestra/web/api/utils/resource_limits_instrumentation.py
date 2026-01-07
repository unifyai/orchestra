"""
Resource pool instrumentation for identifying bottlenecks under load.

Adds OTel span events when bottlenecks are detected:
- DB connection pool delays (checkout wait > threshold)
- DB connection pool overflow
- High file descriptor usage

Key design principle: "Quiet by default" - events only fire when there's
something noteworthy, so traces aren't polluted during normal operation.
"""

import logging
import os
import threading
import time
from typing import Any

from opentelemetry import trace
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration Thresholds
# =============================================================================

# Only emit span events when checkout wait exceeds this threshold (seconds)
POOL_CHECKOUT_WARN_THRESHOLD = 0.1  # 100ms

# Only emit FD warning when usage exceeds this percentage
FD_USAGE_WARN_THRESHOLD = 80  # 80%


# =============================================================================
# DB Connection Pool Instrumentation
# =============================================================================

# Track checkout start times per thread
_checkout_start_times: dict[int, float] = {}
_checkout_lock = threading.Lock()

# Reference to the instrumented pool for gauge reads
_instrumented_pool: Pool | None = None


def _on_do_connect(dbapi_conn: Any, connection_record: Any) -> None:
    """
    Called when a new physical DB connection is created.

    This fires for both regular pool connections AND overflow connections.
    We detect overflow by checking if the pool's overflow count is positive.
    """
    global _instrumented_pool
    if _instrumented_pool is None:
        return

    try:
        overflow_count = _instrumented_pool.overflow()
        if overflow_count > 0:
            span = trace.get_current_span()
            if span and span.is_recording():
                span.add_event(
                    "db.pool.overflow_connection_created",
                    attributes={
                        "db.pool.overflow_count": overflow_count,
                        "db.pool.checked_out": _instrumented_pool.checkedout(),
                        "db.pool.size": _instrumented_pool.size(),
                    },
                )
                logger.warning(
                    f"DB pool overflow: created overflow connection "
                    f"(overflow={overflow_count}, checked_out={_instrumented_pool.checkedout()})",
                )
    except Exception as e:
        logger.debug(f"Failed to record pool overflow event: {e}")


def _on_checkout(
    dbapi_conn: Any,
    connection_record: Any,
    connection_proxy: Any,
) -> None:
    """
    Called when a connection is retrieved from the pool.

    Calculates wait time and emits a span event if it exceeds the threshold.
    """
    thread_id = threading.current_thread().ident
    if thread_id is None:
        return

    with _checkout_lock:
        start_time = _checkout_start_times.pop(thread_id, None)

    if start_time is None:
        return

    wait_time = time.perf_counter() - start_time

    # Only emit event if wait exceeded threshold (quiet by default)
    if wait_time > POOL_CHECKOUT_WARN_THRESHOLD:
        span = trace.get_current_span()
        if span and span.is_recording():
            pool_stats = _get_pool_stats()
            span.add_event(
                "db.pool.checkout_delayed",
                attributes={
                    "db.pool.wait_seconds": round(wait_time, 4),
                    **pool_stats,
                },
            )
            logger.warning(
                f"DB pool checkout delayed: waited {wait_time:.3f}s for connection "
                f"(checked_out={pool_stats.get('db.pool.checked_out', '?')}, "
                f"overflow={pool_stats.get('db.pool.overflow', '?')})",
            )


def _before_checkout_listener(pool: Pool) -> None:
    """
    Record when we start waiting for a connection.

    This is called via the 'checkout' event, but we need to capture the
    timestamp BEFORE the actual checkout happens. We use a pre-checkout
    approach by attaching to the pool's connect event timing.
    """
    thread_id = threading.current_thread().ident
    if thread_id is None:
        return

    with _checkout_lock:
        _checkout_start_times[thread_id] = time.perf_counter()


def _get_pool_stats() -> dict[str, Any]:
    """Get current pool statistics for span attributes."""
    global _instrumented_pool
    if _instrumented_pool is None:
        return {}

    try:
        return {
            "db.pool.checked_out": _instrumented_pool.checkedout(),
            "db.pool.checked_in": _instrumented_pool.checkedin(),
            "db.pool.overflow": _instrumented_pool.overflow(),
            "db.pool.size": _instrumented_pool.size(),
        }
    except Exception:
        return {}


class PoolCheckoutTimer:
    """
    Context manager that wraps pool checkout to measure wait time.

    Usage:
        engine.pool._checkout = PoolCheckoutTimer(engine.pool._checkout)

    This is a cleaner approach than trying to intercept before/after checkout
    via events, since SQLAlchemy's event system doesn't have a "before_checkout".
    """

    def __init__(self, original_checkout):
        self._original_checkout = original_checkout

    def __call__(self, *args, **kwargs):
        thread_id = threading.current_thread().ident
        start_time = time.perf_counter()

        try:
            # Call the original checkout
            result = self._original_checkout(*args, **kwargs)
            return result
        finally:
            if thread_id is not None:
                wait_time = time.perf_counter() - start_time

                # Only emit event if wait exceeded threshold
                if wait_time > POOL_CHECKOUT_WARN_THRESHOLD:
                    span = trace.get_current_span()
                    if span and span.is_recording():
                        pool_stats = _get_pool_stats()
                        span.add_event(
                            "db.pool.checkout_delayed",
                            attributes={
                                "db.pool.wait_seconds": round(wait_time, 4),
                                **pool_stats,
                            },
                        )
                        logger.warning(
                            f"DB pool checkout delayed: waited {wait_time:.3f}s "
                            f"for connection",
                        )


def instrument_db_pool(engine: Engine) -> None:
    """
    Attach instrumentation to the SQLAlchemy engine's connection pool.

    Emits OTel span events when:
    - Connection checkout takes longer than POOL_CHECKOUT_WARN_THRESHOLD
    - An overflow connection is created (pool exhausted)

    Args:
        engine: SQLAlchemy engine to instrument
    """
    global _instrumented_pool
    pool = engine.pool
    _instrumented_pool = pool

    # Listen for new connections (to detect overflow)
    event.listen(pool, "connect", _on_do_connect)

    # Wrap the pool's internal checkout to measure wait time
    # This is more reliable than trying to use before/after events
    if hasattr(pool, "_do_get"):
        original_do_get = pool._do_get

        def timed_do_get(*args, **kwargs):
            thread_id = threading.current_thread().ident
            start_time = time.perf_counter()

            try:
                return original_do_get(*args, **kwargs)
            finally:
                if thread_id is not None:
                    wait_time = time.perf_counter() - start_time

                    if wait_time > POOL_CHECKOUT_WARN_THRESHOLD:
                        span = trace.get_current_span()
                        if span and span.is_recording():
                            pool_stats = _get_pool_stats()
                            span.add_event(
                                "db.pool.checkout_delayed",
                                attributes={
                                    "db.pool.wait_seconds": round(wait_time, 4),
                                    **pool_stats,
                                },
                            )

        pool._do_get = timed_do_get

    logger.info(
        f"Instrumented DB connection pool for bottleneck detection "
        f"(warn threshold: {POOL_CHECKOUT_WARN_THRESHOLD}s)",
    )


# =============================================================================
# File Descriptor Monitoring
# =============================================================================


def check_fd_usage() -> dict[str, Any] | None:
    """
    Check file descriptor usage and return stats if above threshold.

    Returns:
        Dict with FD stats if usage > threshold, None otherwise.
    """
    try:
        import resource

        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

        # Count open FDs
        try:
            # Linux: read from /proc
            fd_dir = f"/proc/{os.getpid()}/fd"
            if os.path.isdir(fd_dir):
                open_fds = len(os.listdir(fd_dir))
            else:
                # macOS/other: no easy way to count, skip
                return None
        except (FileNotFoundError, PermissionError):
            return None

        usage_percent = (open_fds / soft_limit) * 100

        if usage_percent > FD_USAGE_WARN_THRESHOLD:
            return {
                "system.fd.open": open_fds,
                "system.fd.limit": soft_limit,
                "system.fd.usage_percent": round(usage_percent, 1),
            }

        return None

    except Exception:
        return None


def emit_fd_warning_if_needed() -> None:
    """
    Check FD usage and emit a span event if above threshold.

    Call this periodically (e.g., in middleware) to detect FD exhaustion.
    """
    fd_stats = check_fd_usage()
    if fd_stats:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event("system.fd.high_usage", attributes=fd_stats)
            logger.warning(
                f"High file descriptor usage: {fd_stats['system.fd.open']}/"
                f"{fd_stats['system.fd.limit']} ({fd_stats['system.fd.usage_percent']}%)",
            )


# =============================================================================
# Request Concurrency Tracking
# =============================================================================

# Track active requests per worker
_active_requests = 0
_active_requests_lock = threading.Lock()
_peak_concurrent_requests = 0

# Threshold for emitting high concurrency warning
CONCURRENT_REQUESTS_WARN_THRESHOLD = 50


def get_active_request_count() -> int:
    """Get the current number of active requests in this worker."""
    return _active_requests


def get_peak_request_count() -> int:
    """Get the peak concurrent request count seen in this worker."""
    return _peak_concurrent_requests


class RequestTracker:
    """
    Context manager to track request concurrency.

    Emits a span event if concurrent requests exceed threshold.

    Usage:
        with RequestTracker():
            # handle request
    """

    def __enter__(self):
        global _active_requests, _peak_concurrent_requests

        with _active_requests_lock:
            _active_requests += 1
            current = _active_requests
            if current > _peak_concurrent_requests:
                _peak_concurrent_requests = current

        # Emit warning if we're at high concurrency
        if current >= CONCURRENT_REQUESTS_WARN_THRESHOLD:
            span = trace.get_current_span()
            if span and span.is_recording():
                span.add_event(
                    "request.high_concurrency",
                    attributes={
                        "request.concurrent_count": current,
                        "request.worker_pid": os.getpid(),
                    },
                )

        return self

    def __exit__(self, *args):
        global _active_requests

        with _active_requests_lock:
            _active_requests -= 1


# =============================================================================
# Convenience: Combined Bottleneck Check
# =============================================================================


def emit_bottleneck_warnings() -> None:
    """
    Run all bottleneck checks and emit span events for any issues detected.

    Call this in request middleware to get comprehensive bottleneck visibility.
    Currently checks:
    - File descriptor usage
    """
    emit_fd_warning_if_needed()
