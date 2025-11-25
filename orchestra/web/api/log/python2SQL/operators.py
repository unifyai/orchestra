import base64
import io
import json
from typing import Iterable, Optional, Tuple

import imagehash
from fastapi import HTTPException
from pgvector.sqlalchemy import Vector
from PIL import Image
from sqlalchemy import (
    TIMESTAMP,
    BindParameter,
    Boolean,
    Date,
    Float,
    Integer,
    Interval,
    String,
    Text,
    Time,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.expression import Exists, UnaryExpression
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.log_dao import LogDAO

from . import alias_utils
from .core import build_sql_query
from .helpers import (
    _join_subqueries,
    _parse_rhs_list_or_dict_if_needed,
    _select_value,
    _substring_expr,
    cast_expr,
    unify_inferred_types,
)

__all__ = [
    "_handle_logical_operator",
    "_handle_arithmetic_operator",
    "_handle_comparison_operator",
    "_handle_membership_operator",
    "_handle_index_operator",
    "_handle_slice_operator",
    "_handle_l2",
    "_handle_cosine",
    "_handle_ip",
    "_handle_l1",
    "_handle_hamming",
    "_handle_jaccard",
    "_handle_phash_distance",
]


# Value-level OR coalescing helper: returns first truthy value (as text)
def _value_or_coalesce_subq(
    or_filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Build a subquery implementing Python's value-level `x or y`:
    returns x if truthy (non-empty for strings, non-null), else y.
    Always returns a subquery with a text value column.
    """
    from .core import build_sql_query

    lhs_node = or_filter_dict.get("lhs")
    rhs_node = or_filter_dict.get("rhs")

    lhs_expr = build_sql_query(
        lhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )
    rhs_expr = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    lhs_is_sub = isinstance(lhs_expr, Subquery)
    rhs_is_sub = isinstance(rhs_expr, Subquery)

    # Truthiness of LHS
    lhs_truthy = _create_truthiness_condition(lhs_expr, session)

    # Extract string/text values
    if lhs_is_sub:
        lhs_val, _ = _select_value(lhs_expr, session)
        lhs_text = func.replace(cast(lhs_val, String), '"', "")
    else:
        lhs_text = func.replace(cast(lhs_expr, String), '"', "")

    if rhs_is_sub:
        rhs_val, _ = _select_value(rhs_expr, session)
        rhs_text = func.replace(cast(rhs_val, String), '"', "")
    else:
        rhs_text = func.replace(cast(rhs_expr, String), '"', "")

    coalesced = case((lhs_truthy, lhs_text), else_=rhs_text)

    # Build a subquery source to carry log_event_id
    if lhs_is_sub and rhs_is_sub:
        from_clause = lhs_expr.outerjoin(
            rhs_expr,
            lhs_expr.c.log_event_id == rhs_expr.c.log_event_id,
        )
        select_cols = [
            func.coalesce(lhs_expr.c.log_event_id, rhs_expr.c.log_event_id).label(
                "log_event_id",
            ),
            coalesced.label("value"),
            literal("str").label("inferred_type"),
        ]
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(from_clause),
            prefix="value_or",
        )

    base = lhs_expr if lhs_is_sub else rhs_expr
    if isinstance(base, Subquery):
        select_cols = [base.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in base.c.keys():
            select_cols.append(base.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in base.c.keys():
            select_cols.append(base.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [coalesced.label("value"), literal("str").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(base),
            prefix="value_or",
        )

    # Fallback: no subquery on either side, inflate from log_event_alias
    ids_subq = alias_utils.subquery_with_unique_alias(
        select(log_event_alias.id.label("log_event_id")),
        prefix="ids_for_value_or",
    )
    return alias_utils.subquery_with_unique_alias(
        select(
            ids_subq.c.log_event_id,
            coalesced.label("value"),
            literal("str").label("inferred_type"),
        ).select_from(ids_subq),
        prefix="value_or",
    )


def _can_combine_and_conditions(lhs_node, rhs_node):
    """
    Check if two filter nodes can be combined into a single subquery.
    Returns (can_combine, identifier_key) if both are comparison operators
    (excluding 'is'/'is not') on the same identifier, else (False, None).
    """
    if not isinstance(lhs_node, dict) or not isinstance(rhs_node, dict):
        return False, None

    lhs_op = lhs_node.get("operand")
    rhs_op = rhs_node.get("operand")

    # Both must be comparison operators, but NOT 'is' or 'is not'
    # (those are handled specially and shouldn't be combined)
    if lhs_op not in ("==", "!=", "<", ">", "<=", ">=") or rhs_op not in (
        "==",
        "!=",
        "<",
        ">",
        "<=",
        ">=",
    ):
        return False, None

    # Both LHS must be identifiers
    lhs_lhs = lhs_node.get("lhs")
    rhs_lhs = rhs_node.get("lhs")

    if not isinstance(lhs_lhs, dict) or lhs_lhs.get("type") != "identifier":
        return False, None
    if not isinstance(rhs_lhs, dict) or rhs_lhs.get("type") != "identifier":
        return False, None

    lhs_key = lhs_lhs.get("value")
    rhs_key = rhs_lhs.get("value")

    # Both must reference the same identifier
    if lhs_key != rhs_key:
        return False, None

    return True, lhs_key


# Helper function for NULL-safe equality comparisons
def _null_safe_eq(a, b):
    """
    NULL-safe equality comparison for SQLAlchemy expressions using PostgreSQL's
    `IS NOT DISTINCT FROM` semantics:

    - NULL IS NOT DISTINCT FROM NULL  → True
    - NULL IS NOT DISTINCT FROM value → False
    - value IS NOT DISTINCT FROM NULL → False
    - value IS NOT DISTINCT FROM value → True

    This gives us the desired behavior where NULL == NULL evaluates to True,
    while still being hash/merge-joinable for JOIN conditions.
    """
    return a.op("IS NOT DISTINCT FROM")(b)


def _null_safe_ne(a, b):
    """
    NULL-safe inequality comparison for SQLAlchemy expressions using
    PostgreSQL's `IS DISTINCT FROM` semantics, the logical inverse of
    `_null_safe_eq`:

    - NULL IS DISTINCT FROM NULL  → False
    - NULL IS DISTINCT FROM value → True
    - value IS DISTINCT FROM NULL → True
    - value IS DISTINCT FROM value → False
    """
    return a.op("IS DISTINCT FROM")(b)


# Helper function for logical operators (and, or, not)
def _create_truthiness_condition(subq_or_literal, session):
    """
    Takes a subquery or a literal and returns an SQL condition that
    evaluates its "truthiness" in the same way Python does.
    """
    if isinstance(subq_or_literal, (Exists, UnaryExpression)):
        return subq_or_literal

    # If it's a literal value, we can determine truthiness directly in Python.
    if not isinstance(subq_or_literal, Subquery):
        # Let SQLAlchemy handle the boolean conversion for literals
        return literal(
            bool(
                (
                    subq_or_literal.value
                    if isinstance(subq_or_literal, BindParameter)
                    else subq_or_literal
                ),
            ),
        )

    # If it's a subquery, build the condition based on its value and type.
    val_col, val_type = _select_value(subq_or_literal, session)

    # Handle cases where the subquery returns no value (e.g., key does not exist).
    # This should be treated as falsy.
    if val_col is None:
        return literal(False)

    if val_type == "bool":
        # The value column might be JSONB, so we must cast it to Boolean.
        return cast(val_col, Boolean).is_(True)
    elif val_type in ("int", "float"):
        # For numbers, check if not 0
        return case(
            (func.jsonb_typeof(val_col) == "null", literal(False)),
            else_=(cast(val_col, Float) != 0),
        )
    elif val_type == "str":
        # For strings, check if not empty
        return func.length(func.replace(cast(val_col, String), '"', "")) > 0
    elif val_type == "list":
        # For lists, check if not empty
        return func.jsonb_array_length(val_col) > 0
    elif val_type == "dict":
        # For dicts, check if not empty
        return val_col != cast(literal("{}"), JSONB)
    elif val_type == "NoneType":
        # None is always falsy
        return literal(False)
    else:
        # For other types (timestamp, etc.), check if not null
        return val_col.isnot(None)


def _handle_logical_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handles logical operators ('and', 'or', 'not') using CASE statements
    to ensure Python-like short-circuiting.
    """
    operand = filter_dict.get("operand")

    # The 'not' operator can be handled simply by negating the truthiness
    if operand == "not":
        rhs = build_sql_query(
            filter_dict.get("rhs"),
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
        if isinstance(rhs, Subquery):
            # Re-use the truthiness condition, but negate it
            is_truthy_condition = _create_truthiness_condition(rhs, session)
            return alias_utils.subquery_with_unique_alias(
                select(
                    rhs.c.log_event_id.label("log_event_id"),
                    not_(is_truthy_condition).label("value"),
                    literal("bool").label("inferred_type"),
                ).select_from(rhs),
                prefix="logical_not",
            )
        else:
            return not_(rhs)

    # OPTIMIZATION: For AND conditions on the same identifier, combine into single subquery
    # This avoids cartesian products when joining two subqueries for the same identifier
    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    if operand == "and":
        can_combine, identifier_key = _can_combine_and_conditions(lhs_node, rhs_node)
        if can_combine:
            # Build the identifier subquery once
            from .helpers import _build_subquery_for_identifier, _select_value

            identifier_subq = _build_subquery_for_identifier(
                identifier_key,
                log_event_alias,
                alias=f"combined_{identifier_key}",
                log_event_ids=log_event_ids,
                session=session,
                is_derived=is_derived,
                is_vector=is_vector,
            )

            # Extract value column and type from the identifier subquery
            identifier_val, identifier_type = _select_value(identifier_subq, session)

            # Get the RHS values (literals) from the filter dict
            lhs_rhs_node = lhs_node.get("rhs")
            rhs_rhs_node = rhs_node.get("rhs")

            # Build RHS expressions for comparison
            lhs_rhs_expr = build_sql_query(
                lhs_rhs_node,
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
                local_scope=local_scope,
                is_vector=is_vector,
            )
            rhs_rhs_expr = build_sql_query(
                rhs_rhs_node,
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
                local_scope=local_scope,
                is_vector=is_vector,
            )

            # Get RHS values and types
            lhs_rhs_val, lhs_rhs_type = _select_value(lhs_rhs_expr, session)
            rhs_rhs_val, rhs_rhs_type = _select_value(rhs_rhs_expr, session)

            # Build comparison expressions using the same logic as _handle_comparison_operator
            lhs_op = lhs_node.get("operand")
            rhs_op = rhs_node.get("operand")

            # Build comparison expressions using the same logic as _handle_comparison_operator
            # Use cast_expr and unify_inferred_types to handle all type conversions generically
            def _build_comparison_expr(op, lhs_val, lhs_type, rhs_val, rhs_type):
                """Build a comparison expression using standard type unification and casting."""
                final_type = unify_inferred_types(lhs_type, rhs_type)
                lhs_casted = cast_expr(lhs_val, lhs_type, final_type)
                rhs_casted = cast_expr(rhs_val, rhs_type, final_type)

                op_map = {
                    "==": lambda a, b: _null_safe_eq(a, b),
                    "!=": lambda a, b: _null_safe_ne(a, b),
                    "<": lambda a, b: a < b,
                    ">": lambda a, b: a > b,
                    "<=": lambda a, b: a <= b,
                    ">=": lambda a, b: a >= b,
                }
                op_func = op_map.get(op)
                if op_func is None:
                    raise ValueError(f"Unknown comparison operand: {op}")
                return op_func(lhs_casted, rhs_casted)

            lhs_comp_expr = _build_comparison_expr(
                lhs_op,
                identifier_val,
                identifier_type,
                lhs_rhs_val,
                lhs_rhs_type,
            )
            rhs_comp_expr = _build_comparison_expr(
                rhs_op,
                identifier_val,
                identifier_type,
                rhs_rhs_val,
                rhs_rhs_type,
            )

            # Combine both conditions with AND
            combined_condition = and_(lhs_comp_expr, rhs_comp_expr)

            # Create single subquery with both conditions
            return alias_utils.subquery_with_unique_alias(
                select(
                    identifier_subq.c.log_event_id.label("log_event_id"),
                    combined_condition.label("value"),
                    literal("bool").label("inferred_type"),
                )
                .select_from(identifier_subq)
                .where(combined_condition),
                prefix="combined_and",
            )

    # Build LHS and RHS expressions for 'and' / 'or' (original logic)
    lhs = build_sql_query(
        lhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )
    rhs = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    # If neither are subqueries, the operation is happening in a simple WHERE clause
    # where standard `and_` and `or_` are sufficient.
    if not lhs_is_sub and not rhs_is_sub:
        return and_(lhs, rhs) if operand == "and" else or_(lhs, rhs)

    # 1. Define the truthiness conditions for the CASE statement
    lhs_condition = _create_truthiness_condition(lhs, session)
    rhs_condition = _create_truthiness_condition(rhs, session)

    if operand == "and":
        # Logic: CASE WHEN (LHS is truthy) THEN (RHS is truthy) ELSE FALSE END
        case_expr = case((lhs_condition, rhs_condition), else_=literal(False))
    elif operand == "or":
        # Logic: CASE WHEN (LHS is truthy) THEN TRUE ELSE (RHS is truthy) END
        case_expr = case((lhs_condition, literal(True)), else_=rhs_condition)
    else:
        raise ValueError(f"Unknown logical operand: {operand}")

    # 2. Build the final subquery, joining LHS and RHS to evaluate the CASE expression
    select_cols = [case_expr.label("value"), literal("bool").label("inferred_type")]

    from_clause = None
    if lhs_is_sub and rhs_is_sub:
        # Join the two subqueries to have access to both row contexts
        is_full_join = operand == "or"
        from_clause = lhs.outerjoin(
            rhs,
            lhs.c.log_event_id == rhs.c.log_event_id,
            full=is_full_join,
        )
        # Coalesce IDs from both sides in case of outer join
        select_cols.insert(
            0,
            func.coalesce(lhs.c.log_event_id, rhs.c.log_event_id).label("log_event_id"),
        )
    elif lhs_is_sub:
        from_clause = lhs
        select_cols.insert(0, lhs.c.log_event_id.label("log_event_id"))
    elif rhs_is_sub:
        from_clause = rhs
        select_cols.insert(0, rhs.c.log_event_id.label("log_event_id"))
    else:
        return case_expr

    return alias_utils.subquery_with_unique_alias(
        select(*select_cols).select_from(from_clause),
        prefix="logical_op",
    )


def _arithmetic_expr(lval, rval, operand, lval_type, rval_type):
    # Special handling for date/time/timestamp and timedelta arithmetic
    if operand == "+" and lval_type == "datetime" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "datetime"
    elif operand == "-" and lval_type == "datetime" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "datetime"
    elif operand == "-" and lval_type == "datetime" and rval_type == "datetime":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), TIMESTAMP)
        expr = lval - rval
        result_type = "timedelta"
    elif operand == "-" and lval_type == "date" and rval_type == "date":
        lval = cast(cast(lval, Text), Date)
        rval = cast(cast(rval, Text), Date)
        expr = cast(lval, TIMESTAMP) - cast(rval, TIMESTAMP)
        result_type = "timedelta"
    elif operand == "+" and lval_type == "date" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Date)
        rval = cast(rval, Interval)
        expr = lval + rval
        result_type = "date"
    elif operand == "-" and lval_type == "date" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Date)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "date"
    elif operand == "+" and lval_type == "time" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Time)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "time"
    elif operand == "-" and lval_type == "time" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Time)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "time"
    elif (
        operand == "+"
        and lval_type == "timedelta"
        and rval_type in ("datetime", "date", "time")
    ):
        lval = cast(lval, Interval)
        if rval_type == "datetime":
            rval = cast(cast(rval, Text), TIMESTAMP)
        elif rval_type == "date":
            rval = cast(cast(rval, Text), Date)
        else:  # time
            rval = cast(cast(rval, Text), Time)
        expr = lval + rval
        result_type = rval_type
    elif operand == "+" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "timedelta"
    elif operand == "-" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "timedelta"
    elif operand == "*" and lval_type == "timedelta" and rval_type in ("int", "float"):
        lval = cast(cast(lval, Text), Interval)
        rval = cast(rval, Float)
        expr = lval * rval
        result_type = "timedelta"
    elif operand == "*" and lval_type in ("int", "float") and rval_type == "timedelta":
        lval = cast(lval, Float)
        rval = cast(cast(rval, Text), Interval)
        expr = lval * rval
        result_type = "timedelta"
    elif operand == "/" and lval_type == "timedelta" and rval_type in ("int", "float"):
        lval = cast(cast(lval, Text), Interval)
        rval = cast(rval, Float)
        expr = lval / rval
        result_type = "timedelta"
    elif operand == "/" and lval_type == "timedelta" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), Interval)
        rval = cast(cast(rval, Text), Interval)
        expr = func.extract("epoch", lval) / func.extract("epoch", rval)
        result_type = "float"
    else:
        lval = cast_expr(lval, lval_type, rval_type)
        rval = cast_expr(rval, rval_type, lval_type)
        # ──► NEW – make denominator safe for / // %  ◄──
        safe_rval = func.nullif(rval, 0)  # 0   → NULL
        if operand == "+":
            if lval_type == "str" and rval_type == "str":
                lval = func.replace(cast(lval, String), '"', "")
                rval = func.replace(cast(rval, String), '"', "")
                expr = func.concat(lval, rval)
            else:
                expr = lval + rval
        elif operand == "-":
            expr = lval - rval
        elif operand == "*":
            expr = lval * rval
        elif operand == "/":
            # use safe_rval and return NULL when it is NULL
            expr = case((safe_rval.is_(None), None), else_=lval / safe_rval)
        elif operand == "%":
            expr = case((safe_rval.is_(None), None), else_=lval % safe_rval)
        elif operand == "**":
            expr = func.power(lval, rval)
        elif operand == "//":
            expr = case(
                (safe_rval.is_(None), None),
                else_=func.floor(lval / safe_rval),
            )
        result_type = unify_inferred_types(lval_type, rval_type)
    return expr, result_type


# Helper function for arithmetic operators (+, -, *, /, %)
def _handle_arithmetic_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handles arithmetic operators ('+', '-', '*', '**', '//', '/', '%') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the arithmetic operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the arithmetic operator.
    """
    operand = filter_dict.get("operand")
    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    # Rewrite value-level `or` used inside arithmetic into a coalescing subquery
    if isinstance(lhs_node, dict) and lhs_node.get("operand") == "or":
        lhs = _value_or_coalesce_subq(
            lhs_node,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    else:
        lhs = build_sql_query(
            lhs_node,
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    if isinstance(rhs_node, dict) and rhs_node.get("operand") == "or":
        rhs = _value_or_coalesce_subq(
            rhs_node,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    else:
        rhs = build_sql_query(
            rhs_node,
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        return _join_subqueries(lhs, rhs, expr, result_type, session=session)
    elif lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type).label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs),
            prefix="vector_op",
        )
    elif rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type).label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rhs),
            prefix="vector_op",
        )
    else:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        rval = cast_expr(rval, rval_type, lval_type)
        lval = cast_expr(lval, lval_type, rval_type)
        # ──► NEW – safe denominator ◄──
        safe_rval = func.nullif(rval, 0)
        # --------------------------------
        if operand == "+":
            return lval + rval
        elif operand == "-":
            return lval - rval
        elif operand == "*":
            return lval * rval
        elif operand == "/":
            return lval / rval
        elif operand == "%":
            return case((safe_rval.is_(None), None), else_=lval % safe_rval)
        elif operand == "**":
            return func.power(lval, rval)
        elif operand == "//":
            return case(
                (safe_rval.is_(None), None),
                else_=func.floor(lval / safe_rval),
            )


# Helper function for comparison operators (==, !=, <, >, <=, >=, is, is not)
def _handle_comparison_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handles comparison operators ('==', '!=', '<', '>', '<=', '>=', 'is', 'is not') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the comparison operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the comparison operator.
    """
    operand = filter_dict.get("operand")
    lhs_sql = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )
    rhs_sql = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    lval, lval_type = _select_value(lhs_sql, session)
    rval, rval_type = _select_value(rhs_sql, session)

    # --- Build the core boolean expression ---
    # `is` and `is not` are handled specially
    if operand in ("is", "is not"):
        if rval_type == "NoneType":
            # Robust check for `is None` / `is not None` against JSONB
            lval_as_text = cast(lval, Text)
            expr = (
                or_(lval_as_text.is_(None), lval_as_text == "null")
                if operand == "is"
                else and_(lval_as_text.isnot(None), lval_as_text != "null")
            )
        else:
            # For `is True` or `is False`, treat as equality. For other `is` cases, use IS.
            # Note: `val IS TRUE` is the same as `val = TRUE` in SQL for boolean types.
            bool_val = cast_expr(lval, lval_type, "bool")
            expr = (bool_val == rval) if operand == "is" else (bool_val != rval)

    # Handle `==` and `!=` for list comparisons
    elif (
        operand in ("==", "!=")
        and lval_type == "list"
        and isinstance(rhs_sql, BindParameter)
        and isinstance(rhs_sql.value, list)
    ):
        # Explicitly cast lval to JSONB to ensure correct comparison operator
        lval = cast(lval, JSONB)
        # Cast the Python list literal to JSONB for correct comparison
        rval_as_jsonb = cast(literal(json.dumps(rhs_sql.value)), JSONB)
        expr = (lval == rval_as_jsonb) if operand == "==" else (lval != rval_as_jsonb)

    # Handle all other standard comparisons (>, <, ==, etc.)
    else:
        final_type = unify_inferred_types(lval_type, rval_type)
        lval = cast_expr(lval, lval_type, final_type)
        rval = cast_expr(rval, rval_type, final_type)

        # Decide comparison semantics based on context: JOIN vs FILTER.
        # For JOIN conditions we want a plain equality operator so that
        # PostgreSQL can treat it as hash/merge-joinable (required for FULL JOIN).
        # For filters/expressions we use NULL-safe equality/inequality.
        comparison_context = None
        if isinstance(local_scope, dict):
            comparison_context = local_scope.get("__comparison_context__")

        if comparison_context == "join":
            eq_op = lambda a, b: a == b
            ne_op = lambda a, b: a != b
        else:
            eq_op = _null_safe_eq
            ne_op = _null_safe_ne

        op_map = {
            "==": eq_op,
            "!=": ne_op,
            "<": lambda a, b: a < b,
            ">": lambda a, b: a > b,
            "<=": lambda a, b: a <= b,
            ">=": lambda a, b: a >= b,
        }
        if operand not in op_map:
            raise ValueError(f"Unknown comparison operand: {operand}")
        expr = op_map[operand](lval, rval)

    # --- Wrap the expression in a subquery or join as needed ---
    lhs_is_sub = isinstance(lhs_sql, Subquery)
    rhs_is_sub = isinstance(rhs_sql, Subquery)

    if lhs_is_sub and rhs_is_sub:
        # If both sides are subqueries, they MUST be joined to avoid a cartesian product.
        return _join_subqueries(lhs_sql, rhs_sql, expr, "bool", session=session)
    elif lhs_is_sub:
        # If only LHS is a subquery, build the subquery from it.
        select_cols = [lhs_sql.c.log_event_id.label("log_event_id")]
        # (propagate comprehension indices if they exist)
        if "__comp_idx__" in lhs_sql.c.keys():
            select_cols.append(lhs_sql.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs_sql.c.keys():
            select_cols.append(lhs_sql.c.__parent_idx__.label("__parent_idx__"))

        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs_sql),
            prefix="comparison_op",
        )
    elif rhs_is_sub:
        # Symmetrical case if only RHS is a subquery.
        select_cols = [rhs_sql.c.log_event_id.label("log_event_id")]
        # (propagate comprehension indices if they exist)
        if "__comp_idx__" in rhs_sql.c.keys():
            select_cols.append(rhs_sql.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs_sql.c.keys():
            select_cols.append(rhs_sql.c.__parent_idx__.label("__parent_idx__"))

        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rhs_sql),
            prefix="comparison_op",
        )
    else:
        # If neither side was a subquery, we can return the raw boolean clause.
        return expr


# Helper function for membership operators (in, not in)
def _handle_membership_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handles membership operators ('in', 'not in') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the membership operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the membership operator.
    """
    operand = filter_dict.get("operand")
    is_in = operand == "in"

    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    # Both sides are subqueries
    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)

        # Check if RHS is a JSONB list for containment check
        if rval_type == "list" and is_in:
            # Use PostgreSQL's @> operator for array containment
            condition = rval.op("@>")(func.jsonb_build_array(lval))
            expr = ~condition if not is_in else condition
        elif lval_type == "list" and is_in:
            # Use PostgreSQL's @> operator for array containment
            condition = lval.op("@>")(func.jsonb_build_array(rval))
            expr = ~condition if not is_in else condition
        else:
            # Fall back to substring check for non-list types
            condition = _substring_expr(lval, rval)
            expr = ~condition if not is_in else condition

        return _join_subqueries(lhs, rhs, expr, "bool", session=session)

    # Only LHS is a subquery
    elif lhs_is_sub and not rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)

        # Check if we're trying to do membership test on a boolean column
        if lval_type == "bool" and not isinstance(lval, list):
            raise HTTPException(
                status_code=400,
                detail="Invalid membership test on a boolean column. Use equality check (==) instead of 'in'.",
            )

        # Handle JSONB array containment for list columns
        if lval_type == "list":
            # If RHS is a BindParameter or literal, we can use the @> operator
            if isinstance(rhs, BindParameter) or not isinstance(
                rhs,
                (list, dict, Subquery),
            ):
                # Create a JSON array with the single value for the containment check
                rhs_value = rhs.value if isinstance(rhs, BindParameter) else rhs

                # Use PostgreSQL's @> operator for array containment
                containment_expr = lval.op("@>")(func.jsonb_build_array(rhs_value))
                expr = containment_expr if is_in else ~containment_expr
                select_cols = [lhs.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in lhs.c.keys():
                    select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [expr.label("value"), literal("bool").label("inferred_type")],
                )
                return alias_utils.subquery_with_unique_alias(
                    select(*select_cols).select_from(lhs),
                    prefix="membership_op",
                )

        # Fall back to standard handling for non-array types
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        if rhs_list and isinstance(rhs_list, list):
            if lval_type == "NoneType" and rhs_list:
                # If the LHS column is all NULLs, its type is ambiguous.
                # We infer the type from the RHS list and select the corresponding typed column.
                first_item_type = LogDAO.infer_type("", rhs_list[0])

                # Based on the inferred type, we select the correct column from the subquery `lhs`.
                if first_item_type == "str":
                    lval = lhs.c.str_value
                elif first_item_type == "int":
                    lval = lhs.c.int_value
                elif first_item_type == "float":
                    lval = lhs.c.float_value
                elif first_item_type == "bool":
                    lval = lhs.c.bool_value
            expr = lval.in_(rhs_list) if is_in else ~lval.in_(rhs_list)
        else:
            substring_cond = _substring_expr(lval, rhs)
            expr = substring_cond if is_in else ~substring_cond

        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs),
            prefix="membership_op",
        )

    # Only RHS is a subquery
    elif rhs_is_sub and not lhs_is_sub:
        rval, rval_type = _select_value(rhs, session)

        # Check if we're trying to do membership test on a boolean column
        if rval_type == "bool" and not isinstance(rval, list):
            raise HTTPException(
                status_code=400,
                detail="Invalid membership test on a boolean column. Use equality check (==) instead of 'in'.",
            )

        # Handle the case where RHS is a JSONB array and LHS is a scalar value to check for containment
        if rval_type == "list":
            # If LHS is a scalar value (not a list or subquery), we can use the @> operator
            if not isinstance(lhs, (list, dict, Subquery)):
                lhs_value = lhs.value if isinstance(lhs, BindParameter) else lhs
                # TODO: this can be avoided with more robust parsing/tokenization (AST based)
                try:
                    lhs_value = json.loads(lhs_value)
                except:
                    pass

                # Use PostgreSQL's @> operator for array containment
                # Create a JSONB array with the single value for the containment check
                containment_expr = rval.op("@>")(func.jsonb_build_array(lhs_value))
                cond = containment_expr if is_in else ~containment_expr
                select_cols = [rhs.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in rhs.c.keys():
                    select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
                select_cols.extend(
                    [cond.label("value"), literal("bool").label("inferred_type")],
                )
                return alias_utils.subquery_with_unique_alias(
                    select(*select_cols).select_from(rhs),
                    prefix="membership_op",
                )

        lhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("lhs"), lhs)

        if lhs_list is not None and isinstance(lhs_list, list):
            cond = rval.in_(lhs_list) if is_in else ~rval.in_(lhs_list)
        else:
            # Substring check. We'll check: "lhs in str(rval)" => substring.
            substring_cond = _substring_expr(lhs, rval)
            cond = substring_cond if is_in else ~substring_cond

        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [cond.label("value"), literal("bool").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rhs),
            prefix="membership_op",
        )

    # Neither side is a subquery
    else:
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        # If we successfully parse a list, do normal membership
        if rhs_list is not None and isinstance(rhs_list, list):
            return lhs.in_(rhs_list) if is_in else ~lhs.in_(rhs_list)

        # Otherwise do substring check
        substring_cond = _substring_expr(lhs, rhs)
        return substring_cond if is_in else ~substring_cond


def _handle_index_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handle the INDEX operator in a filter expression.

    Args:
        filter_dict (dict): The filter expression dictionary containing "lhs" and "rhs".
        log_event_alias: The alias for the log event.
        session: The database session.

    Returns:
        Subquery: A subquery that extracts the sub-value from the LHS JSON object/array using the RHS key/index.
    """
    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    lhs_expr = build_sql_query(
        lhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )
    rhs_expr = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    if isinstance(lhs_expr, Subquery):
        lhs_valcol, lhs_type = _select_value(lhs_expr, session)

        # If the LHS type does not support indexing, the result is None.
        if lhs_type not in ("list", "dict", "str"):
            extracted = literal(None)
            inferred_type = literal("NoneType").label("inferred_type")
        elif isinstance(rhs_expr, Subquery):
            rhs_valcol, rhs_type = _select_value(rhs_expr, session)
            # JSONB indexing using PostgreSQL operators:
            #  - arrays: jsonb -> int
            #  - objects: jsonb -> text
            if lhs_type == "list":
                # Prefer integer index; non-integer indexes will yield NULL
                extracted = lhs_valcol.op("->")(cast(rhs_valcol, Integer))
            elif lhs_type == "dict":
                extracted = lhs_valcol.op("->")(cast(rhs_valcol, String))
            else:
                extracted = literal(None)
            # Infer result type from jsonb value at runtime
            json_type = func.jsonb_typeof(extracted)
            inferred_type = case(
                (
                    json_type == "number",
                    case(
                        (cast(extracted, Text).like("%.%"), literal("float")),
                        else_=literal("int"),
                    ),
                ),
                (json_type == "string", literal("str")),
                (json_type == "boolean", literal("bool")),
                (json_type == "null", literal("NoneType")),
                (json_type == "array", literal("list")),
                (json_type == "object", literal("dict")),
                else_=literal("NoneType"),
            ).label("inferred_type")
            select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend([extracted.label("value"), inferred_type])
            subq = (
                select(*select_cols)
                .select_from(lhs_expr)
                .join(rhs_expr, lhs_expr.c.log_event_id == rhs_expr.c.log_event_id)
            )
            return alias_utils.subquery_with_unique_alias(subq, prefix="index_op")
        else:
            rhs_expr_val = (
                rhs_expr.value if isinstance(rhs_expr, BindParameter) else rhs_expr
            )

            if lhs_type == "str":
                # Indexing a string gives a single character (string)
                if isinstance(rhs_expr_val, int):
                    pg_index = rhs_expr_val + 1
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        literal(pg_index),
                        1,
                    )
                else:
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        cast(rhs_expr_val, Integer) + 1,
                        1,
                    )
                inferred_type = literal("str").label("inferred_type")
            else:
                # Indexing a JSONB list/dict using PostgreSQL operators
                if isinstance(rhs_expr_val, int):
                    extracted = lhs_valcol.op("->")(literal(rhs_expr_val))
                else:
                    extracted = lhs_valcol.op("->")(cast(rhs_expr_val, String))
                json_type = func.jsonb_typeof(extracted)
                inferred_type = case(
                    (
                        json_type == "number",
                        case(
                            (cast(extracted, Text).like("%.%"), literal("float")),
                            else_=literal("int"),
                        ),
                    ),
                    (json_type == "string", literal("str")),
                    (json_type == "boolean", literal("bool")),
                    (json_type == "null", literal("NoneType")),
                    (json_type == "array", literal("list")),
                    (json_type == "object", literal("dict")),
                    else_=literal("NoneType"),
                ).label("inferred_type")

        select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs_expr.c.keys():
            select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs_expr.c.keys():
            select_cols.append(lhs_expr.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend([extracted.label("value"), inferred_type])
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs_expr),
            prefix="contains_op",
        )

    else:
        # If LHS is not a subquery => e.g. LHS is a python dict or list literal
        if isinstance(lhs_expr, (dict, list)):
            if isinstance(rhs_expr, (int, str)):
                try:
                    extracted_value = lhs_expr[rhs_expr]
                except (KeyError, IndexError, TypeError):
                    extracted_value = None
                return literal(extracted_value)
            else:
                raise ValueError(
                    "Cannot index a python dict/list with a subquery or complex expr.",
                )
        else:
            raise ValueError(
                "INDEX operator expects LHS to be a subquery (JSON) or a python list/dict literal.",
            )


def _handle_slice_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
):
    """
    Handle the SLICE operator in a filter expression.

    Args:
        filter_dict (dict): The filter expression dictionary containing "lhs" and "rhs".
        log_event_alias: The alias for the log event.
        session: The database session.

    Returns:
        Subquery: A subquery that extracts the substring from the LHS string or subarray from the LHS list
        using the slice bounds.
    """
    lhs_node = filter_dict.get("lhs")
    rhs_bounds = filter_dict.get("rhs")

    # Unpack the slice bounds
    lower, upper = rhs_bounds

    lhs_expr = build_sql_query(
        lhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    if isinstance(lhs_expr, Subquery):
        lhs_valcol, lhs_type = _select_value(lhs_expr, session)

        select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs_expr.c.keys():
            select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs_expr.c.keys():
            select_cols.append(lhs_expr.c.__parent_idx__.label("__parent_idx__"))

        if lhs_type == "str":
            # Text value without the JSON quotes Postgres adds
            str_txt = func.replace(cast(lhs_valcol, String), '"', "")
            str_len = func.char_length(str_txt)
            if lower is None:
                start_expr = literal(1)
                lower_index_expr = literal(0)  # 0-based mirror
            elif isinstance(lower, int) and lower >= 0:
                start_expr = literal(lower + 1)  # 1-based
                lower_index_expr = literal(lower)
            elif isinstance(lower, int):  # negative
                start_expr = str_len + literal(lower) + literal(1)
                lower_index_expr = str_len + literal(lower)  # 0-based
            else:
                raise ValueError("Slice start must be int or None")

            if upper is None:
                extracted = func.substring(str_txt, start_expr)
            elif isinstance(upper, int) and upper >= 0:
                slice_len = max(upper - (lower or 0), 0)
                extracted = func.substring(str_txt, start_expr, literal(slice_len))
            elif isinstance(upper, int):  # negative stop
                end_index_expr = str_len + literal(upper)
                slice_len_expr = end_index_expr - lower_index_expr
                extracted = func.substring(str_txt, start_expr, slice_len_expr)
            else:
                raise ValueError("Slice stop must be int or None")

            select_cols.extend(
                [
                    extracted.label("value"),
                    literal("str").label("inferred_type"),
                ],
            )
        elif lhs_type == "list":
            # For JSONB arrays, use jsonb_path_query_array to extract the slice
            # JSON path is 0-indexed, so we use the bounds directly
            start = lower
            end = upper - 1  # End is inclusive in JSON path

            # Use PostgreSQL's jsonb_path_query_array function with JSON path expression
            slice_expr = func.jsonb_path_query_array(
                lhs_valcol,
                literal_column(f"'$[{start} to {end}]'"),
            ).label("value")

            select_cols.extend(
                [
                    slice_expr,
                    literal("list").label("inferred_type"),
                ],
            )
        else:
            raise ValueError(
                "Slice operation is only supported on string or list values",
            )

        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs_expr),
            prefix="contains_op",
        )
    else:
        # If LHS is a Python literal, perform the slice operation in Python
        if isinstance(lhs_expr, str):
            extracted_value = lhs_expr[lower:upper]
            return literal(extracted_value)
        elif isinstance(lhs_expr, list):
            extracted_value = lhs_expr[lower:upper]
            return literal(extracted_value)
        else:
            raise ValueError(
                "SLICE operator expects LHS to be a subquery (string or list) or a Python string/list literal.",
            )


# Helper functions for vector binary ops
def _ensure_numeric_iterable(name: str, val: Iterable) -> Tuple[list, int]:
    try:
        seq = list(val)
    except Exception:
        raise TypeError(
            f"{name}: expected a numeric iterable, got {type(val).__name__}.",
        )
    if not seq:
        raise ValueError(f"{name}: empty vector is not allowed.")
    try:
        vec = [float(x) for x in seq]
    except Exception:
        raise ValueError(f"{name}: vector must contain only numeric values.")
    return vec, len(vec)


def _literal_vector(vec: Iterable[float], dim: int) -> ClauseElement:
    return cast(literal(list(vec), type_=ARRAY(Float())), Vector(dim))


def _coerce_to_vector_sql(
    expr: object,
    inferred_type: Optional[str],
    side_label: str,
) -> ClauseElement:
    if hasattr(expr, "op"):
        if inferred_type == "list":
            return cast(expr.op("#>>")("{}"), Vector())
        return expr
    if inferred_type == "list" and isinstance(expr, (list, tuple)):
        vec, dim = _ensure_numeric_iterable(side_label, expr)
        return _literal_vector(vec, dim)
    if isinstance(expr, (list, tuple)):
        vec, dim = _ensure_numeric_iterable(side_label, expr)
        return _literal_vector(vec, dim)
    raise TypeError(
        f"Cosine/Distance operand {side_label}: expected a vector-compatible value "
        f"(numeric list/tuple or SQL expression), got {type(expr).__name__}.",
    )


def _vector_binary_op(
    lhs_src: ClauseElement | Subquery | object,
    rhs_src: ClauseElement | Subquery | object,
    session,
    operator_symbol: str,
    result_type_label: str,
    subquery_prefix: str,
) -> ClauseElement | Subquery:
    lhs_is_sub = isinstance(lhs_src, Subquery)
    rhs_is_sub = isinstance(rhs_src, Subquery)

    def _value_from_source(src, side_name: str):
        # Always delegate to _select_value to get (value, inferred_type)
        val, val_type = _select_value(src, session, is_vector=True)
        return (
            _coerce_to_vector_sql(val, val_type, side_name),
            val_type,
            (src if isinstance(src, Subquery) else None),
        )

    lval, _, lsub = _value_from_source(lhs_src, "lhs")
    rval, _, rsub = _value_from_source(rhs_src, "rhs")

    expr = lval.op(operator_symbol)(rval).cast(Float)

    # Both sides subqueries
    if lsub is not None and rsub is not None:
        return _join_subqueries(lsub, rsub, expr, result_type_label, session=session)

    # Only LHS is subquery
    if lsub is not None:
        select_cols = [lsub.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lsub.c.keys():
            select_cols.append(lsub.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lsub.c.keys():
            select_cols.append(lsub.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type_label).label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lsub),
            prefix=subquery_prefix,
        )

    # Only RHS is subquery
    if rsub is not None:
        select_cols = [rsub.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rsub.c.keys():
            select_cols.append(rsub.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rsub.c.keys():
            select_cols.append(rsub.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(result_type_label).label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rsub),
            prefix=subquery_prefix,
        )

    # Neither side subquery
    return expr


def _handle_l2(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles L2 distance operator between two vector operands: v1 <-> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<->", "float", "l2_distance")


def _handle_cosine(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles cosine similarity operator between two vector operands: v1 <=> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<=>", "float", "cosine_similarity")


def _handle_ip(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles inner product operator between two vector operands: v1 <#> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<#>", "float", "inner_product")


def _handle_l1(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles L1 distance operator between two vector operands: v1 <+> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<+>", "float", "l1_distance")


def _handle_hamming(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles Hamming distance operator between two vector operands: v1 <~> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<~>", "float", "hamming_distance")


def _handle_jaccard(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles Jaccard distance operator between two vector operands: v1 <%> v2.
    """
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=True,
    )
    return _vector_binary_op(lhs, rhs, session, "<%>", "float", "jaccard_distance")


def _handle_phash_distance(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles Hamming distance operator between two pHash hex strings.
    """

    def compute_phash_from_base64(b64_string):
        """Computes pHash from a base64 string, returning the integer value."""
        try:
            # Remove data URI prefix if present
            if "," in b64_string:
                b64_string = b64_string.split(",")[1]

            image_data = base64.b64decode(b64_string)
            image = Image.open(io.BytesIO(image_data))
            hash_value = imagehash.phash(image)
            return format(int(str(hash_value), 16), "016x")
        except Exception:
            return None

    lhs_dict = filter_dict.get("lhs")
    rhs_dict = filter_dict.get("rhs")

    # Check for raw image literals and compute their pHash on the fly
    if isinstance(lhs_dict, dict) and lhs_dict.get("type") == "image":
        lhs_phash = compute_phash_from_base64(lhs_dict["value"])
        lhs = literal(lhs_phash) if lhs_phash is not None else literal(None)
    else:
        lhs = build_sql_query(
            lhs_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )

    if isinstance(rhs_dict, dict) and rhs_dict.get("type") == "image":
        rhs_phash = compute_phash_from_base64(rhs_dict["value"])
        rhs = literal(rhs_phash) if rhs_phash is not None else literal(None)
    else:
        rhs = build_sql_query(
            rhs_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        expr = func.hamming_distance(cast(lval, Text), cast(rval, Text))
        return _join_subqueries(lhs, rhs, expr, "int", session=session)

    if lhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        expr = func.hamming_distance(cast(lval, Text), cast(rval, Text))
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("int").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(lhs),
            prefix="phash_distance",
        )

    if rhs_is_sub:
        rval, _ = _select_value(rhs, session)
        lval, _ = _select_value(lhs, session)
        expr = func.hamming_distance(cast(lval, Text), cast(rval, Text))
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("int").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rhs),
            prefix="phash_distance",
        )

    return func.hamming_distance(cast(lhs, Text), cast(rhs, Text))
