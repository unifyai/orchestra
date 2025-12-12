"""
Temporal utility functions for datetime operations in SQL queries.

This module consolidates timezone stripping and naive timestamp building
logic that was previously duplicated across multiple locations in jsonb_builder.py.
"""

from typing import Any, Optional

__all__ = [
    "strip_timezone_sql",
    "strip_timezone_and_cast",
    "build_naive_timestamp_for_identifier",
    "build_naive_timestamp_for_base",
    "build_naive_timestamp_expr",
    "build_naive_datetime_subtraction_subquery",
    "strip_timezone_from_value",
]

from sqlalchemy import TIMESTAMP, Text, cast, func, literal, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ClauseElement

from orchestra.db.models.orchestra_models import LogEvent

from . import alias_utils
from .ast_utils import (
    is_base_node,
    is_identifier_node,
    is_literal_node,
    parse_base_params,
)


def strip_timezone_sql(text_expr: ClauseElement) -> ClauseElement:
    """
    Strip timezone information from a datetime string expression.

    Handles all common timezone formats:
    - Z suffix (UTC)
    - +HH:MM or -HH:MM offsets
    - +HH or -HH offsets (without minutes)

    Args:
        text_expr: SQLAlchemy expression that evaluates to a text datetime string

    Returns:
        Expression with timezone stripped, cast to naive TIMESTAMP
    """
    # First strip +/-HH:MM or +/-HH offset patterns
    stripped = func.regexp_replace(text_expr, r"[+-]\d{2}(:\d{2})?$", "", "g")
    # Then strip Z suffix
    stripped = func.regexp_replace(stripped, r"Z$", "", "g")
    return cast(stripped, TIMESTAMP)


def strip_timezone_and_cast(text_expr: ClauseElement) -> ClauseElement:
    """
    Strip timezone from text expression and cast to naive TIMESTAMP.

    This is the primary function to use for datetime subtraction operations
    where we want to compare "wall clock" times rather than UTC instants.

    Args:
        text_expr: SQLAlchemy expression that evaluates to a text datetime string

    Returns:
        Naive TIMESTAMP expression
    """
    return strip_timezone_sql(text_expr)


def build_naive_timestamp_for_identifier(
    key: str,
    log_event_alias,
) -> ClauseElement:
    """
    Build a naive TIMESTAMP expression for an identifier (field) reference.

    Extracts the raw text from JSONB and uses strip_timezone_and_cast
    to strip any timezone info and cast to naive TIMESTAMP.

    Args:
        key: The field name/key to extract from JSONB data
        log_event_alias: SQLAlchemy alias for LogEvent table

    Returns:
        Naive TIMESTAMP expression extracted from JSONB data with timezone stripped
    """
    raw_text = log_event_alias.data.op("->>")(key)
    return strip_timezone_and_cast(raw_text)


def build_naive_timestamp_for_base(
    event_ids: list,
    key: str,
) -> Optional[ClauseElement]:
    """
    Build a naive TIMESTAMP expression for a BASE reference.

    Creates a scalar subquery that extracts the raw text from JSONB and
    uses strip_timezone_and_cast to strip any timezone info and cast to
    naive TIMESTAMP.

    Args:
        event_ids: List of log event IDs to reference
        key: The field name/key to extract from JSONB data

    Returns:
        Naive TIMESTAMP scalar subquery with timezone stripped, or None if event_ids is empty
    """
    if not event_ids:
        return None

    ref_id = event_ids[0]
    safe_key = key.replace("/", "_")
    ref_log_event = aliased(LogEvent, name=f"base_ref_{safe_key}")

    raw_text_subq = (
        select(ref_log_event.data.op("->>")(key).label("raw_ts"))
        .where(ref_log_event.id == ref_id)
        .scalar_subquery()
    )
    return strip_timezone_and_cast(raw_text_subq)


def build_naive_timestamp_expr(
    node: Any,
    log_event_alias,
) -> Optional[ClauseElement]:
    """
    Build a naive TIMESTAMP expression for an AST node.

    Unified builder that handles identifier, BASE, and literal node types.
    All datetime values are processed through strip_timezone_and_cast to
    ensure consistent timezone handling.

    Supported node types:
    - identifier: Direct field reference (extracts from JSONB data)
    - BASE: Reference to another log event's field
    - literal: A literal datetime string value

    Args:
        node: AST node (identifier, BASE expression, or literal)
        log_event_alias: SQLAlchemy alias for LogEvent table

    Returns:
        Naive TIMESTAMP expression with timezone stripped, or None if node type is not supported
    """
    if is_identifier_node(node):
        key = node.get("value")
        if key:
            return build_naive_timestamp_for_identifier(key, log_event_alias)
        return None

    if is_base_node(node):
        event_ids, key = parse_base_params(node)
        if event_ids and key:
            return build_naive_timestamp_for_base(event_ids, key)
        return None

    if is_literal_node(node):
        # For literal datetime strings, strip timezone and cast to naive TIMESTAMP
        value = node.get("value")
        if value is not None:
            return strip_timezone_and_cast(literal(str(value)))
        return None

    # Unsupported node type
    return None


def build_naive_datetime_subtraction_subquery(
    lhs_key: str,
    rhs_key: str,
    lhs_base_ids: list,
    rhs_base_ids: list,
    lhs_expr=None,
    lhs_is_sub: bool = False,
) -> ClauseElement:
    """
    Build a subquery for datetime subtraction between two BASE expressions.

    This handles the "wall clock" time comparison where we strip timezones
    before subtraction to compare local times rather than UTC instants.

    Args:
        lhs_key: Left-hand side field key
        rhs_key: Right-hand side field key
        lhs_base_ids: Event IDs for left-hand side
        rhs_base_ids: Event IDs for right-hand side
        lhs_expr: Optional left-hand side subquery (for preserving comp indices)
        lhs_is_sub: Whether lhs_expr is a subquery

    Returns:
        Subquery with timedelta result
    """
    from sqlalchemy import and_, literal

    # Create aliased log event tables
    lhs_log = aliased(LogEvent, name=f"lhs_naive_{lhs_key.replace('/', '_')}")
    rhs_log = aliased(LogEvent, name=f"rhs_naive_{rhs_key.replace('/', '_')}")

    # Get raw text, strip timezone, cast to naive TIMESTAMP
    lhs_raw = lhs_log.data.op("->>")(lhs_key)
    lhs_naive = strip_timezone_sql(lhs_raw)

    rhs_raw = rhs_log.data.op("->>")(rhs_key)
    rhs_naive = strip_timezone_sql(rhs_raw)

    # Build the subtraction result
    expr = lhs_naive - rhs_naive

    # Build subquery columns
    select_cols = [
        lhs_log.id.label("log_event_id"),
        expr.label("value"),
        literal("timedelta").label("inferred_type"),
    ]

    # Preserve comprehension indices if present
    if lhs_is_sub and lhs_expr is not None and hasattr(lhs_expr.c, "__comp_idx__"):
        select_cols.insert(1, lhs_expr.c.__comp_idx__.label("__comp_idx__"))
    if lhs_is_sub and lhs_expr is not None and hasattr(lhs_expr.c, "__parent_idx__"):
        select_cols.insert(-2, lhs_expr.c.__parent_idx__.label("__parent_idx__"))

    # Build the query - both logs should reference the same ID
    where_clause = and_(
        lhs_log.id.in_(lhs_base_ids),
        rhs_log.id.in_(rhs_base_ids),
        lhs_log.id == rhs_log.id,
    )

    result_subq = alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(lhs_log, rhs_log).where(where_clause),
        prefix="datetime_naive_sub",
    )
    return result_subq


def strip_timezone_from_value(value_expr: ClauseElement) -> ClauseElement:
    """
    Strip timezone from an already-converted value expression.

    This is a fallback for cases where we can't access the raw JSONB text.
    Converts to text, strips timezone, and casts back to naive TIMESTAMP.

    Args:
        value_expr: SQLAlchemy expression (may be TIMESTAMPTZ)

    Returns:
        Naive TIMESTAMP expression
    """
    text_expr = cast(value_expr, Text)
    return strip_timezone_sql(text_expr)
