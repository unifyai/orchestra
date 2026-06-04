"""
Subquery utility functions for building result subqueries.

This module provides a single function to build result subqueries that
preserve log_event_id and comprehension indices (__comp_idx__, __parent_idx__),
replacing 22+ duplicate patterns across jsonb_builder.py.
"""

__all__ = [
    "build_result_subquery",
    "build_result_subquery_with_join",
    "build_result_subquery_with_groupby",
    "get_subquery_columns",
    "has_comprehension_index",
    "has_parent_index",
]

from sqlalchemy import literal, select
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.selectable import Subquery

from . import alias_utils


def build_result_subquery(
    base_subq: Subquery,
    value_expr: ClauseElement,
    result_type: str,
    prefix: str = "result",
) -> Subquery:
    """
    Build a result subquery that wraps a value expression.

    This is the standard pattern for creating result subqueries that:
    1. Preserve log_event_id for correlation
    2. Preserve __comp_idx__ if present (for comprehensions)
    3. Preserve __parent_idx__ if present (for nested comprehensions)
    4. Add value and inferred_type columns

    Args:
        base_subq: The source subquery to wrap
        value_expr: The expression for the value column
        result_type: The inferred type string (e.g., "bool", "int", "float")
        prefix: Prefix for the generated subquery alias

    Returns:
        A new subquery with standardized columns
    """
    select_cols = [base_subq.c.log_event_id.label("log_event_id")]

    if "__comp_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__comp_idx__.label("__comp_idx__"))

    if "__parent_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__parent_idx__.label("__parent_idx__"))

    select_cols.extend(
        [
            value_expr.label("value"),
            literal(result_type).label("inferred_type"),
        ],
    )

    return alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(base_subq),
        prefix=prefix,
    )


def build_result_subquery_with_join(
    base_subq: Subquery,
    join_target,
    join_condition: ClauseElement,
    value_expr: ClauseElement,
    result_type: str,
    prefix: str = "result",
) -> Subquery:
    """
    Build a result subquery with a JOIN to another table.

    This is used when the value expression references columns from both
    the base subquery and another table (e.g., log_event_alias for JSONB fields).

    Args:
        base_subq: The source subquery to wrap
        join_target: The table/alias to join with
        join_condition: The JOIN condition (e.g., base_subq.c.log_event_id == log_event_alias.id)
        value_expr: The expression for the value column (may reference both tables)
        result_type: The inferred type string (e.g., "bool", "int", "float")
        prefix: Prefix for the generated subquery alias

    Returns:
        A new subquery with standardized columns
    """
    select_cols = [base_subq.c.log_event_id.label("log_event_id")]

    if "__comp_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__comp_idx__.label("__comp_idx__"))

    if "__parent_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__parent_idx__.label("__parent_idx__"))

    select_cols.extend(
        [
            value_expr.label("value"),
            literal(result_type).label("inferred_type"),
        ],
    )

    from_clause = base_subq.join(join_target, join_condition)

    return alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(from_clause),
        prefix=prefix,
    )


def build_result_subquery_with_groupby(
    base_subq: Subquery,
    value_expr: ClauseElement,
    result_type: str,
    prefix: str = "aggregated",
    include_value_in_groupby: bool = False,
) -> Subquery:
    """
    Build a result subquery with GROUP BY for aggregation operations.

    Similar to build_result_subquery but adds GROUP BY clause for:
    - log_event_id
    - __comp_idx__ if present
    - __parent_idx__ if present
    - Optionally the value column

    Args:
        base_subq: The source subquery to wrap
        value_expr: The aggregation expression for the value column
        result_type: The inferred type string
        prefix: Prefix for the generated subquery alias
        include_value_in_groupby: Whether to include the value column in GROUP BY

    Returns:
        A new subquery with standardized columns and GROUP BY
    """
    select_cols = [base_subq.c.log_event_id.label("log_event_id")]
    group_by_cols = [base_subq.c.log_event_id]

    if "__comp_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__comp_idx__.label("__comp_idx__"))
        group_by_cols.append(base_subq.c.__comp_idx__)

    if "__parent_idx__" in base_subq.c.keys():
        select_cols.append(base_subq.c.__parent_idx__.label("__parent_idx__"))
        group_by_cols.append(base_subq.c.__parent_idx__)

    if include_value_in_groupby and "value" in base_subq.c.keys():
        group_by_cols.append(base_subq.c.value)

    select_cols.extend(
        [
            value_expr.label("value"),
            literal(result_type).label("inferred_type"),
        ],
    )

    return alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(base_subq).group_by(*group_by_cols),
        prefix=prefix,
    )


def get_subquery_columns(
    subq: Subquery,
) -> tuple:
    """
    Get the standard columns from a subquery for building new subqueries.

    Returns a tuple of (select_cols, has_comp_idx, has_parent_idx) where
    select_cols is a list starting with log_event_id.

    Args:
        subq: A subquery to inspect

    Returns:
        Tuple of (base select columns list, has_comp_idx, has_parent_idx)
    """
    select_cols = [subq.c.log_event_id.label("log_event_id")]
    has_comp_idx = "__comp_idx__" in subq.c.keys()
    has_parent_idx = "__parent_idx__" in subq.c.keys()

    if has_comp_idx:
        select_cols.append(subq.c.__comp_idx__.label("__comp_idx__"))

    if has_parent_idx:
        select_cols.append(subq.c.__parent_idx__.label("__parent_idx__"))

    return select_cols, has_comp_idx, has_parent_idx


def has_comprehension_index(subq: Subquery) -> bool:
    """
    Check if a subquery has a __comp_idx__ column.

    Args:
        subq: A subquery to inspect

    Returns:
        True if the subquery has a __comp_idx__ column
    """
    return "__comp_idx__" in subq.c.keys()


def has_parent_index(subq: Subquery) -> bool:
    """
    Check if a subquery has a __parent_idx__ column.

    Args:
        subq: A subquery to inspect

    Returns:
        True if the subquery has a __parent_idx__ column
    """
    return "__parent_idx__" in subq.c.keys()
