import datetime
import hashlib
import logging
import re
import time
from collections import defaultdict
from typing import Any, AsyncGenerator, Dict, Generator

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.web.api.utils.observability import (
    get_first_name,
    get_last_name,
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


def convert_datetimes(obj):
    """Convert datetime objects to ISO 8601 strings."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()  # Convert datetime to ISO 8601 string
    elif isinstance(obj, dict):
        # If the object is a dictionary, recursively convert datetime objects in its values
        return {key: convert_datetimes(value) for key, value in obj.items()}
    elif isinstance(obj, (list, tuple)):
        # If the object is a list, recursively convert datetime objects in its elements
        return [convert_datetimes(item) for item in obj]
    else:
        return obj


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
                first_name=getattr(request_state, "first_name", None),
                last_name=getattr(request_state, "last_name", None),
            )
        if hasattr(request_state, "request_id"):
            set_request_id(request_state.request_id)

        # Initialize sql_trace list if not present
        if not hasattr(request_state, "sql_trace"):
            request_state.sql_trace = []

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
    # Truncate statement to avoid hitting collector limits
    truncated_statement = (
        statement[:4000] + "..." if len(statement) > 4000 else statement
    )
    span.set_attribute("db.statement", truncated_statement)

    # Add query parameters, with truncation
    if parameters:
        try:
            # Convert parameters to a string representation
            params_str = str(parameters)
            if len(params_str) > 1024:
                params_str = params_str[:1024] + "..."
            span.set_attribute("db.parameters", params_str)
        except Exception:
            # In case of serialization errors, record a placeholder
            span.set_attribute("db.parameters", "[unserializable]")

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

    # Store query details in request state for sql trace if available
    if hasattr(conn, "info") and "request_state" in conn.info:
        request_state = conn.info["request_state"]
        if hasattr(request_state, "sql_trace"):
            # Create a trace entry with initial data

            trace_entry = {
                "query": statement,
                "parameters": convert_datetimes(parameters),
                "query_type": query_type,
                "table": table,
                "start_time": time.time(),
                "query_fingerprint": query_fingerprint,
            }
            # Store the trace entry in context for later retrieval
            context._sql_trace_entry = trace_entry


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
        first_name = get_first_name()
        last_name = get_last_name()

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

                span.end()

        # Complete the SQL trace entry and add it to request.state.sql_trace
        if (
            hasattr(conn, "info")
            and "request_state" in conn.info
            and hasattr(context, "_sql_trace_entry")
        ):
            request_state = conn.info["request_state"]
            if hasattr(request_state, "sql_trace"):
                trace_entry = context._sql_trace_entry
                # Add duration and other completion data
                trace_entry["duration"] = duration
                trace_entry["duration_ms"] = duration * 1000
                if hasattr(cursor, "rowcount"):
                    trace_entry["rows_affected"] = cursor.rowcount

                # Add the completed trace entry to the list
                request_state.sql_trace.append(trace_entry)
                request_state.first_name = first_name
                request_state.last_name = last_name
                request_state.email = user_email
                request_state.user_id = user_id

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


@event.listens_for(Session, "after_begin")
def _attach_request_state(session: Session, transaction, connection):
    if "request_state" in session.info:
        connection.info["request_state"] = session.info["request_state"]


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """
    Create and get database session.

    :param request: current request.
    :yield: database session.
    """
    SessionLocal = request.app.state.db_session_factory
    session: Session = SessionLocal()
    session.info["request_state"] = request.state
    try:  # noqa: WPS501
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


async def get_async_db_session(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Create and get async database session.

    :param request: current request.
    :yield: async database session.
    """
    async_session_factory = request.app.state.async_session_factory
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            raise e


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
