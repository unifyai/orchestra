import hashlib
import logging
import re
import time
from collections import defaultdict
from typing import Any, Dict, Generator

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import event, text
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

            # For very slow queries (>500ms), capture detailed performance information
            if duration > 0.5 and query_type == "select":
                try:
                    # 1. Log the original query and parameters (sanitized)
                    log_extras["query_text"] = statement
                    # Sanitize parameters to avoid sensitive data in logs
                    if parameters:
                        if isinstance(parameters, (list, tuple)):
                            # For positional parameters, just log the count
                            log_extras["param_count"] = len(parameters)
                        elif isinstance(parameters, dict):
                            # For named parameters, log the keys
                            log_extras["param_keys"] = list(parameters.keys())

                    # 2. Get table statistics
                    if table != "unknown":
                        try:
                            # Use text() for proper parameter binding
                            stats_query = text(
                                "SELECT * FROM pg_stat_user_tables WHERE relname = :table_name",
                            )
                            stats_result = conn.execute(
                                stats_query,
                                {"table_name": table},
                            )
                            stats_row = stats_result.fetchone()
                            if stats_row:
                                # Convert row to dict
                                table_stats = dict(zip(stats_result.keys(), stats_row))
                                log_extras["table_stats"] = {
                                    "total_rows": table_stats.get("n_live_tup", 0),
                                    "dead_rows": table_stats.get("n_dead_tup", 0),
                                    "sequential_scans": table_stats.get("seq_scan", 0),
                                    "index_scans": table_stats.get("idx_scan", 0),
                                    "sequential_rows_read": table_stats.get(
                                        "seq_tup_read",
                                        0,
                                    ),
                                    "index_rows_fetched": table_stats.get(
                                        "idx_tup_fetch",
                                        0,
                                    ),
                                    "inserts": table_stats.get("n_tup_ins", 0),
                                    "updates": table_stats.get("n_tup_upd", 0),
                                    "deletes": table_stats.get("n_tup_del", 0),
                                    "last_vacuum": table_stats.get("last_vacuum"),
                                    "last_analyze": table_stats.get("last_analyze"),
                                }
                        except Exception as stats_error:
                            print(f"Error getting table stats: {stats_error}")
                            log_extras["table_stats_error"] = str(stats_error)

                    # 3. Get index information
                    if table != "unknown":
                        try:
                            # Use text() for proper parameter binding
                            index_query = text(
                                """
                            SELECT
                                i.relname as index_name,
                                a.attname as column_name,
                                ix.indisunique as is_unique,
                                ix.indisprimary as is_primary
                            FROM
                                pg_class t,
                                pg_class i,
                                pg_index ix,
                                pg_attribute a
                            WHERE
                                t.oid = ix.indrelid
                                AND i.oid = ix.indexrelid
                                AND a.attrelid = t.oid
                                AND a.attnum = ANY(ix.indkey)
                                AND t.relkind = 'r'
                                AND t.relname = :table_name
                            ORDER BY
                                t.relname,
                                i.relname;
                            """,
                            )
                            index_result = conn.execute(
                                index_query,
                                {"table_name": table},
                            )
                            indexes = []
                            for row in index_result:
                                indexes.append(
                                    {
                                        "index_name": row[0],
                                        "column_name": row[1],
                                        "is_unique": row[2],
                                        "is_primary": row[3],
                                    },
                                )
                            log_extras["table_indexes"] = indexes
                        except Exception as index_error:
                            print(f"Error getting index info: {index_error}")
                            log_extras["index_info_error"] = str(index_error)

                    # 4. Check for missing indexes based on query pattern
                    try:
                        # Extract WHERE conditions to suggest potential indexes
                        where_pattern = re.search(
                            r"WHERE\s+(.+?)(?:ORDER BY|GROUP BY|LIMIT|$)",
                            statement,
                            re.IGNORECASE | re.DOTALL,
                        )
                        if where_pattern:
                            where_clause = where_pattern.group(1).strip()
                            # Extract column names from WHERE clause
                            column_pattern = re.findall(
                                r"([a-zA-Z0-9_\.]+)\s*(?:=|>|<|LIKE|IN)",
                                where_clause,
                                re.IGNORECASE,
                            )
                            potential_index_columns = []
                            for col in column_pattern:
                                if "." in col:
                                    # Handle table.column format
                                    potential_index_columns.append(col.split(".")[1])
                                else:
                                    potential_index_columns.append(col)

                            if potential_index_columns:
                                log_extras[
                                    "potential_index_columns"
                                ] = potential_index_columns
                    except Exception as pattern_error:
                        log_extras["pattern_analysis_error"] = str(pattern_error)

                    # 5. Check for table bloat (tables that need VACUUM)
                    try:
                        # Use text() for proper parameter binding
                        bloat_query = text(
                            """
                        SELECT
                            current_database(), schemaname, tablename,
                            ROUND(CASE WHEN otta=0 THEN 0.0 ELSE sml.relpages/otta::numeric END,1) AS bloat_ratio,
                            CASE WHEN relpages < otta THEN 0 ELSE relpages::bigint - otta::bigint END AS bloat_pages
                        FROM (
                            SELECT
                                schemaname, tablename, cc.reltuples, cc.relpages, bs,
                                CEIL((cc.reltuples*((datahdr+ma-
                                    (CASE WHEN datahdr%ma=0 THEN ma ELSE datahdr%ma END))+nullhdr2+4))/(bs-20::float)) AS otta
                            FROM (
                                SELECT
                                    ma,bs,schemaname,tablename,
                                    (datawidth+(hdr+ma-(case when hdr%ma=0 THEN ma ELSE hdr%ma END)))::numeric AS datahdr,
                                    (maxfracsum*(nullhdr+ma-(case when nullhdr%ma=0 THEN ma ELSE nullhdr%ma END))) AS nullhdr2
                                FROM (
                                    SELECT
                                        schemaname, tablename, hdr, ma, bs,
                                        SUM((1-null_frac)*avg_width) AS datawidth,
                                        MAX(null_frac) AS maxfracsum,
                                        hdr+(
                                            SELECT 1+count(*)/8
                                            FROM pg_stats s2
                                            WHERE null_frac<>0 AND s2.schemaname = s.schemaname AND s2.tablename = s.tablename
                                        ) AS nullhdr
                                    FROM pg_stats s, (
                                        SELECT
                                            (SELECT current_setting('block_size')::numeric) AS bs,
                                            CASE WHEN substring(v,12,3) IN ('8.0','8.1','8.2') THEN 27 ELSE 23 END AS hdr,
                                            CASE WHEN v ~ 'mingw32' THEN 8 ELSE 4 END AS ma
                                        FROM (SELECT version() AS v) AS foo
                                    ) AS constants
                                    GROUP BY 1,2,3,4,5
                                ) AS foo
                            ) AS foo
                            JOIN pg_class cc ON cc.relname = tablename
                            JOIN pg_namespace nn ON cc.relnamespace = nn.oid AND nn.nspname = schemaname
                            WHERE schemaname = 'public'
                        ) AS sml
                        WHERE sml.tablename = :table_name
                        """,
                        )
                        try:
                            bloat_result = conn.execute(
                                bloat_query,
                                {"table_name": table},
                            )
                            bloat_row = bloat_result.fetchone()
                            if bloat_row:
                                log_extras["table_bloat"] = {
                                    "bloat_ratio": bloat_row[3],
                                    "bloat_pages": bloat_row[4],
                                }
                        except Exception as e:
                            # This query might fail on some PostgreSQL versions, so we'll silently ignore errors
                            pass
                    except Exception as bloat_error:
                        log_extras["bloat_analysis_error"] = str(bloat_error)

                    # 6. Performance recommendations based on collected data
                    recommendations = []

                    # Check if we're doing sequential scans on large tables
                    if (
                        log_extras.get("table_stats", {}).get("sequential_scans", 0) > 0
                        and log_extras.get("table_stats", {}).get("total_rows", 0)
                        > 1000
                    ):
                        recommendations.append(
                            "Consider adding indexes to avoid sequential scans on large tables",
                        )

                    # Check for high bloat ratio
                    if log_extras.get("table_bloat", {}).get("bloat_ratio", 0) > 3:
                        recommendations.append(
                            f"Table has high bloat ratio ({log_extras['table_bloat']['bloat_ratio']}). Consider running VACUUM FULL",
                        )

                    # Check for potential missing indexes
                    if (
                        "potential_index_columns" in log_extras
                        and "table_indexes" in log_extras
                    ):
                        indexed_columns = set()
                        for idx in log_extras["table_indexes"]:
                            indexed_columns.add(idx["column_name"])

                        for col in log_extras["potential_index_columns"]:
                            if col not in indexed_columns:
                                recommendations.append(
                                    f"Consider adding index on column '{col}'",
                                )

                    if recommendations:
                        log_extras["performance_recommendations"] = recommendations

                except Exception as e:
                    log_extras["analysis_error"] = str(e)

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
