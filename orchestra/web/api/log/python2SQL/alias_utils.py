"""
Utility module for generating unique subquery aliases.

This module provides thread-safe, collision-free alias generation using
a monotonic counter approach. It ensures that all subqueries in the
python2SQL system have unique names, preventing DuplicateAlias errors
in complex nested queries.
"""

import itertools

from sqlalchemy.sql.selectable import Select, Subquery

# Thread-safe counter for generating unique alias suffixes
_alias_counter = itertools.count(1)


def unique_alias(prefix: str = "subq") -> str:
    """
    Generate a unique alias for a subquery.

    Args:
        prefix: Semantic prefix for the alias (e.g., "join_subq", "event_ids")

    Returns:
        A unique alias string like "join_subq_1a", "event_ids_2b", etc.
        Uses hexadecimal counter values to keep names short.

    Examples:
        >>> unique_alias("join_subq")
        'join_subq_1'
        >>> unique_alias("event_ids")
        'event_ids_2'
    """
    counter_value = next(_alias_counter)
    # Use hex to keep the suffix short even for large counter values
    suffix = hex(counter_value)[2:]  # Remove '0x' prefix
    alias = f"{prefix}_{suffix}"

    # Validate PostgreSQL's 63-character identifier limit
    if len(alias) > 63:
        # Truncate prefix if needed, preserving uniqueness via suffix
        max_prefix_len = 63 - len(suffix) - 1  # -1 for underscore
        truncated_prefix = prefix[:max_prefix_len]
        alias = f"{truncated_prefix}_{suffix}"

    return alias


def subquery_with_unique_alias(
    selectable: Select,
    prefix: str = "subq",
) -> Subquery:
    """
    Create a subquery with an automatically generated unique alias.

    This is a convenience wrapper around SQLAlchemy's .subquery() method
    that ensures the subquery has a unique name.

    Args:
        selectable: The SQLAlchemy Select object to convert to a subquery
        prefix: Semantic prefix for the alias

    Returns:
        A SQLAlchemy Subquery object with a unique alias

    Examples:
        >>> stmt = select(LogEvent.id).where(LogEvent.status == 'active')
        >>> subq = subquery_with_unique_alias(stmt, "active_events")
        # Creates subquery with alias like "active_events_3c"
    """
    alias = unique_alias(prefix)
    return selectable.subquery(name=alias)
