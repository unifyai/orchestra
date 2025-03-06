import hashlib
import logging
import re
import time
from collections import defaultdict
from typing import Any, Dict, Generator

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.logging import structured_logger
from orchestra.web.api.utils.observability import (
    get_request_id,
    get_user_email,
    get_user_id,
    record_db_query_duration,
    set_request_id,
    set_user_context,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("orchestra.db")

# Dictionary to store query start times by connection id
query_start_times: Dict[int, float] = {}

# Dictionary to track active transactions by connection id
active_transactions: Dict[int, Any] = {}

# TTL for query_start_times (in seconds) to prevent memory leaks
QUERY_START_TIMES_TTL = 60  # 1 minute

# Cleanup function for query_start_times to prevent memory leaks
def cleanup_query_start_times():
    """Remove stale entries from query_start_times to prevent memory leaks."""
    current_time = time.time()
    to_remove = []

    for conn_id, start_time in query_start_times.items():
        if current_time - start_time > QUERY_START_TIMES_TTL:
            to_remove.append(conn_id)

    for conn_id in to_remove:
        query_start_times.pop(conn_id, None)


# Track query counts by table and type for pattern detection
query_patterns = defaultdict(int)


def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Event hook that fires before SQL execution.

    This hook captures the start time of each query and creates an OpenTelemetry span
    with user context for tracing. Each database query is linked to the user who initiated
    the request, enabling full observability from API request through database operations.

    Args:
        conn: SQLAlchemy connection
        cursor: Database cursor
        statement: SQL statement to be executed
        parameters: Bound parameters
        context: Execution context
        executemany: True if executemany is used
    """
    # Get user context from request state if available
    if hasattr(conn, "info") and "request_state" in conn.info:
        request_state = conn.info["request_state"]
        if hasattr(request_state, "user_id"):
            set_user_context(
                user_id=request_state.user_id,
                user_email=getattr(request_state, "user_email", None),
            )
        if hasattr(request_state, "request_id"):
            set_request_id(request_state.request_id)

    # Periodically clean up stale entries
    if len(query_start_times) > 100:  # Only check when we have many entries
        cleanup_query_start_times()

    conn_id = id(conn)
    query_start_times[conn_id] = time.time()

    # Extract query type using regex (SELECT, INSERT, UPDATE, DELETE, etc.)
    query_type = "unknown"
    match = re.match(
        r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|BEGIN|COMMIT)",
        statement.upper(),
    )
    if match:
        query_type = match.group(1).lower()

    # Track transaction boundaries
    if query_type == "begin":
        active_transactions[conn_id] = {
            "start_time": time.time(),
            "queries": [],
            "span": tracer.start_span(
                name="db.transaction",
                kind=SpanKind.CLIENT,
            ),
        }

    # Get user information from context
    user_id = get_user_id() or "anonymous"
    user_email = get_user_email()

    # Get request ID from context
    request_id = get_request_id()

    # Extract table name
    table = "unknown"
    if query_type in ["select", "insert", "update", "delete"]:
        table_match = re.search(
            r"(?:FROM|INTO|UPDATE)\s+([a-zA-Z0-9_\.]+)",
            statement.upper(),
        )
        if table_match:
            table = table_match.group(1).lower()

    # Start OpenTelemetry span
    current_span = trace.get_current_span()
    parent_context = current_span.get_span_context()

    span = tracer.start_span(
        name=f"db.query.{query_type}.{table}",
        kind=SpanKind.CLIENT,
    )

    # Add attributes to span - including standardized user context
    span.set_attribute("db.system", "postgresql")
    span.set_attribute("db.statement", statement)
    span.set_attribute("db.query_type", query_type)
    span.set_attribute("db.table", table)
    span.set_attribute("db.user_id", user_id)
    span.set_attribute("user.id", user_id)
    if user_email:
        span.set_attribute("db.user_email", user_email)
        span.set_attribute("user.email", user_email)
    if request_id:
        span.set_attribute("request.id", request_id)

    # Add query fingerprint for slow query analysis
    query_fingerprint = _fingerprint_query(statement)
    span.set_attribute("db.query_fingerprint", query_fingerprint)
    context._query_fingerprint = query_fingerprint

    # Store span in context
    context._otel_span = span

    # If this is part of a transaction, add to the transaction's query list
    if conn_id in active_transactions:
        active_transactions[conn_id]["queries"].append(
            {
                "query_type": query_type,
                "table": table,
                "start_time": time.time(),
                "fingerprint": query_fingerprint,
                "statement": statement,
            },
        )


def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    """Event hook that fires after SQL execution.

    This hook calculates query duration, records metrics, and logs query details.
    For slow queries (>100ms), it logs the complete query text and parameters.
    All logs include user context and trace identifiers to enable correlation
    between API requests, user actions, and database operations in observability tools.

    Args:
        conn: SQLAlchemy connection
        cursor: Database cursor
        statement: SQL statement that was executed
        parameters: Bound parameters
        context: Execution context
        executemany: True if executemany is used
    """
    conn_id = id(conn)
    start_time = query_start_times.pop(conn_id, None)

    if start_time:
        duration = time.time() - start_time

        # Extract query type
        query_type = "unknown"
        match = re.match(
            r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|BEGIN|COMMIT)",
            statement.upper(),
        )
        if match:
            query_type = match.group(1).lower()

        # Extract table name (simplified approach)
        table = "unknown"
        if query_type in ["select", "insert", "update", "delete"]:
            table_match = re.search(
                r"(?:FROM|INTO|UPDATE)\s+([a-zA-Z0-9_\.]+)",
                statement.upper(),
            )
            if table_match:
                table = table_match.group(1).lower()

        # Get query fingerprint
        query_fingerprint = getattr(
            context,
            "_query_fingerprint",
            _fingerprint_query(statement),
        )

        # Record metrics
        user_id = get_user_id() or "anonymous"
        user_email = get_user_email()
        request_id = get_request_id()

        # Use the enhanced record_db_query_duration function
        record_db_query_duration(
            query_type=query_type,
            table=table,
            duration=duration,
            query_fingerprint=query_fingerprint,
        )

        # Get current trace information
        current_span = trace.get_current_span()
        raw_trace_id = current_span.get_span_context().trace_id
        raw_span_id = current_span.get_span_context().span_id
        trace_id = f"{raw_trace_id:032x}"
        span_id = f"{raw_span_id:016x}"
        # Determine if this is a slow query (over 100ms)
        is_slow_query = duration > 0.1
        log_level = logging.WARNING if is_slow_query else logging.DEBUG

        # Log query information with more details for slow queries
        log_extras = {
            "query_type": query_type,
            "table": table,
            "duration": duration,
            "duration_ms": duration * 1000,
            "user_id": user_id,
            "traceID": trace_id,
            "spanID": span_id,
            "query_fingerprint": query_fingerprint,
        }

        if user_email:
            log_extras["user_email"] = user_email

        if request_id:
            log_extras["request_id"] = request_id

        # Include full query text for slow queries
        if is_slow_query:
            log_extras["query_text"] = statement
            log_extras["query_params"] = str(parameters)

            # For slow queries, try to detect locks
            try:
                # Check for locks in pg_stat_activity
                lock_check_query = """
                SELECT count(*) as lock_count
                FROM pg_stat_activity
                WHERE wait_event_type = 'Lock'
                """
                lock_result = conn.execute(lock_check_query)
                lock_count = lock_result.scalar() or 0

                if lock_count > 0:
                    log_extras["locks_detected"] = True
                    log_extras["locks_count"] = lock_count

                    # Get more details about locks if they exist
                    lock_details_query = """
                    SELECT blocked_statement, blocking_statement,
                           blocked_duration
                    FROM pg_stat_activity a
                    JOIN pg_locks blocked_locks ON a.pid = blocked_locks.pid
                    JOIN pg_locks blocking_locks ON blocked_locks.transactionid = blocking_locks.transactionid
                        AND blocked_locks.pid != blocking_locks.pid
                    JOIN pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
                    WHERE NOT blocked_locks.granted
                    LIMIT 5
                    """
                    try:
                        lock_details = conn.execute(lock_details_query)
                        lock_info = [dict(row) for row in lock_details]
                        if lock_info:
                            log_extras["lock_details"] = lock_info
                    except Exception as e:
                        log_extras["lock_details_error"] = str(e)
            except Exception as e:
                log_extras["lock_check_error"] = str(e)

            # For very slow queries (>500ms), capture EXPLAIN plan
            if duration > 0.5 and query_type == "select":
                try:
                    explain_query = (
                        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"
                    )
                    explain_cursor = conn.execute(explain_query, parameters)
                    explain_plan = explain_cursor.scalar()
                    log_extras["explain_plan"] = explain_plan
                except Exception as e:
                    log_extras["explain_error"] = str(e)

        if is_slow_query:
            structured_logger.warning(
                f"DB Query: {query_type} on {table} (SLOW)",
                extra=log_extras,
            )

        # End OpenTelemetry span if it exists
        if hasattr(context, "_otel_span"):
            span = context._otel_span
            if span.is_recording():
                span.set_attribute("db.duration_ms", duration * 1000)

                # Add slow query marker for filtering in dashboards
                if is_slow_query:
                    span.set_attribute("db.slow_query", True)

                # Add row count information if available
                if hasattr(cursor, "rowcount"):
                    span.set_attribute("db.rows_affected", cursor.rowcount)

                # Add lock information if detected
                if "locks_detected" in log_extras:
                    span.set_attribute("db.locks_detected", True)
                    span.set_attribute("db.locks_count", log_extras["locks_count"])

                span.end()

        # Handle transaction completion
        if query_type == "commit" and conn_id in active_transactions:
            transaction = active_transactions.pop(conn_id)
            transaction_span = transaction["span"]

            # Calculate total transaction duration
            transaction_duration = time.time() - transaction["start_time"]

            # Add transaction attributes
            transaction_span.set_attribute(
                "db.transaction.duration_ms",
                transaction_duration * 1000,
            )
            transaction_span.set_attribute(
                "db.transaction.query_count",
                len(transaction["queries"]),
            )

            # Add information about tables involved in the transaction
            tables = set()
            for query in transaction["queries"]:
                if query["table"] != "unknown":
                    tables.add(query["table"])

            transaction_span.set_attribute("db.transaction.tables", list(tables))

            # Add user context
            transaction_span.set_attribute("user.id", user_id)
            if user_email:
                transaction_span.set_attribute("user.email", user_email)
            if request_id:
                transaction_span.set_attribute("request.id", request_id)

            # End the transaction span
            transaction_span.end()


def register_db_listeners():
    """Register SQLAlchemy event listeners."""
    from orchestra.web.lifetime import get_engine

    try:
        # Get the engine from the application state
        engine = get_engine()

        # Register event listeners on the engine instead of Session
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        event.listen(engine, "after_cursor_execute", after_cursor_execute)
        logger.info("Successfully registered SQLAlchemy event listeners")
    except RuntimeError as e:
        logger.warning(f"Could not register SQLAlchemy event listeners: {e}")
    except Exception as e:
        logger.error(f"Error registering SQLAlchemy event listeners: {e}")


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """
    Create and get database session.

    :param request: current request.
    :yield: database session.
    """
    session: Session = request.app.state.db_session_factory()
    # Store request state in connection info for context propagation
    if session.bind and hasattr(session.bind, "engine"):
        connection = session.bind.engine.connect()
        connection.info["request_state"] = request.state
        session.bind = connection
    try:  # noqa: WPS501
        yield session
    # TODO: Fix this, it catches all exceptions instead of just the db ones
    # except Exception as e:
    #     digest = hashlib.shake_256(str(e).encode()).digest(4).hex()
    #     logger.error(f"Digest {digest}: {e}")
    #     raise HTTPException(
    #         status_code=500,  # noqa: WPS432
    #         detail=f"Internal Server Error. Digest: {digest}",
    #     )
    finally:
        session.commit()
        session.close()


def _fingerprint_query(statement: str) -> str:
    """
    Create a fingerprint of a SQL query to identify similar queries.

    This function normalizes a SQL query by:
    1. Removing specific literal values
    2. Standardizing whitespace
    3. Removing comments
    4. Hashing the result for a compact representation

    Args:
        statement: SQL statement to fingerprint

    Returns:
        A hash string representing the query pattern
    """
    if not statement:
        return "empty_query"

    # Normalize the query
    # 1. Convert to lowercase
    normalized = statement.lower()

    # 2. Remove comments
    normalized = re.sub(r"--.*?$", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"/\*.*?\*/", "", normalized, flags=re.DOTALL)

    # 3. Replace literal values with placeholders
    normalized = re.sub(r"'[^']*'", "'?'", normalized)  # String literals
    normalized = re.sub(r"\b\d+\b", "?", normalized)  # Number literals

    # 4. Normalize whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # 5. Replace IN lists with a standard placeholder
    normalized = re.sub(r"IN\s*\([^)]+\)", "IN (?)", normalized, flags=re.IGNORECASE)

    # 6. Hash the normalized query
    fingerprint = hashlib.md5(normalized.encode()).hexdigest()

    # 7. Add a prefix with the query type for easier identification
    match = re.match(
        r"^\s*(select|insert|update|delete|create|alter|drop|truncate|begin|commit)",
        normalized,
    )
    if match:
        query_type = match.group(1)
        return f"{query_type}_{fingerprint[:12]}"

    return fingerprint[:16]
