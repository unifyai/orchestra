import json

from fastapi import HTTPException
from sqlalchemy import (
    TIMESTAMP,
    BindParameter,
    Date,
    Float,
    Integer,
    Interval,
    String,
    Text,
    Time,
    and_,
    cast,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.log_dao import LogDAO

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
]


# Helper function for logical operators (and, or, not)
def _handle_logical_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
):
    """
    Handles logical operators ('and', 'or', 'not') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the logical operator and operands.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        Subquery or SQLAlchemy condition based on the logical operator.
    """
    operand = filter_dict.get("operand")
    lhs = (
        build_sql_query(
            filter_dict.get("lhs"),
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )
        if operand != "not"
        else None
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    # Check if lhs and rhs are subqueries
    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    def _true_ids(subq):
        val_col, val_type = _select_value(subq, session)

        if val_type == "bool":
            # For boolean types, check if the value is True
            condition = val_col.is_(True)
        elif val_type in ("int", "float"):
            # For numeric types, check if the value is not zero
            condition = val_col != 0
        elif val_type == "str":
            # For string types, check if not empty (after removing JSON quotes)
            condition = func.length(func.replace(cast(val_col, String), '"', "")) > 0
        elif val_type in ("list", "dict"):
            # For collections, check if not empty
            if val_type == "list":
                condition = func.jsonb_array_length(val_col) > 0
            else:  # dict
                # Check if dict is not empty using simple comparison
                condition = val_col != cast(literal("{}"), JSONB)
        elif val_type == "NoneType":
            # None is always falsy
            condition = literal(False)
        else:
            # For other types (timestamp, date, time, timedelta), check if not null
            condition = subq.c.value.isnot(None)

        return select(subq.c.log_event_id).select_from(subq).where(condition)

    def _make_bool_subq(ids_selectable):
        tmp = ids_selectable.subquery()
        return (
            select(
                tmp.c.log_event_id.label("log_event_id"),
                literal(True).label("value"),
                literal("bool").label("inferred_type"),
            )
            .select_from(tmp)
            .subquery()
        )

    # Handle "not"
    if operand == "not":
        if rhs_is_sub:
            val_col, val_type = _select_value(rhs, session)

            if val_type == "bool":
                # For boolean types, negate the value
                not_expr = not_(val_col)
            elif val_type in ("int", "float"):
                # For numeric types, check if the value is zero
                not_expr = val_col == 0
            elif val_type == "str":
                # For string types, check if empty (after removing JSON quotes)
                not_expr = (
                    func.length(func.replace(cast(val_col, String), '"', "")) == 0
                )
            elif val_type in ("list", "dict"):
                # For collections, check if empty
                if val_type == "list":
                    not_expr = func.jsonb_array_length(val_col) == 0
                else:  # dict
                    # Check if dict is empty using simple comparison
                    not_expr = val_col == cast(literal("{}"), JSONB)
            elif val_type == "NoneType":
                # None is always falsy, so not None is True
                not_expr = literal(True)
            else:
                # For other types (timestamp, date, time, timedelta), check if null
                not_expr = not_(rhs.c.value)

            return (
                select(
                    rhs.c.log_event_id.label("log_event_id"),
                    not_expr.label("value"),
                    literal("bool").label("inferred_type"),
                )
                .select_from(rhs)
                .subquery()
            )
        else:
            return not_(rhs)

    # Handle "and"/"or"
    if operand in ("and", "or"):
        if lhs_is_sub and rhs_is_sub:
            lhs_ids = _true_ids(lhs)
            rhs_ids = _true_ids(rhs)
            combined_ids = (
                lhs_ids.intersect(rhs_ids)
                if operand == "and"
                else lhs_ids.union(rhs_ids)
            )
            return _make_bool_subq(combined_ids)

        elif lhs_is_sub and not rhs_is_sub:
            if operand == "and":
                passed_ids = _true_ids(lhs).subquery()
                filtered_ids = (
                    select(passed_ids.c.log_event_id.label("log_event_id"))
                    .join(
                        log_event_alias,
                        log_event_alias.id == passed_ids.c.log_event_id,
                    )
                    .where(rhs)
                )
                return _make_bool_subq(filtered_ids)
            else:
                passed_ids = _true_ids(lhs)
                pass_rhs = select(log_event_alias.id.label("log_event_id")).where(rhs)
                combined = passed_ids.union(pass_rhs)
                return _make_bool_subq(combined)

        elif not lhs_is_sub and rhs_is_sub:
            if operand == "and":
                passed_ids = _true_ids(rhs).subquery()
                filtered_ids = (
                    select(passed_ids.c.log_event_id.label("log_event_id"))
                    .join(
                        log_event_alias,
                        log_event_alias.id == passed_ids.c.log_event_id,
                    )
                    .where(lhs)
                )
                return _make_bool_subq(filtered_ids)
            else:
                pass_rhs = _true_ids(rhs)
                pass_lhs = select(log_event_alias.id.label("log_event_id")).where(lhs)
                combined = pass_lhs.union(pass_rhs)
                return _make_bool_subq(combined)

        else:
            return and_(lhs, rhs) if operand == "and" else or_(lhs, rhs)

    raise ValueError(f"Unknown logical operand: {operand}")


def _arithmetic_expr(lval, rval, operand, lval_type, rval_type):
    # Special handling for date/time/timestamp and timedelta arithmetic
    if operand == "+" and lval_type == "timestamp" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval + rval
        result_type = "timestamp"
    elif operand == "-" and lval_type == "timestamp" and rval_type == "timedelta":
        lval = cast(cast(lval, Text), TIMESTAMP)
        rval = cast(cast(rval, Text), Interval)
        expr = lval - rval
        result_type = "timestamp"
    elif operand == "-" and lval_type == "timestamp" and rval_type == "timestamp":
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
        and rval_type in ("timestamp", "date", "time")
    ):
        lval = cast(lval, Interval)
        if rval_type == "timestamp":
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
            expr = lval / rval
        elif operand == "%":
            expr = lval % rval
        elif operand == "**":
            expr = func.power(lval, rval)
        elif operand == "//":
            expr = func.floor(lval / rval)
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
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
        return select(*select_cols).select_from(lhs).subquery()
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
        return select(*select_cols).select_from(rhs).subquery()
    else:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        rval = cast_expr(rval, rval_type, lval_type)
        lval = cast_expr(lval, lval_type, rval_type)
        if operand == "+":
            return lval + rval
        elif operand == "-":
            return lval - rval
        elif operand == "*":
            return lval * rval
        elif operand == "/":
            return lval / rval
        elif operand == "%":
            return lval % rval
        elif operand == "**":
            return func.power(lval, rval)
        elif operand == "//":
            return func.floor(lval / rval)


# Helper function for comparison operators (==, !=, <, >, <=, >=, is, is not)
def _handle_comparison_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
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
    lhs = build_sql_query(
        filter_dict.get("lhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        lval = cast_expr(lval, lval_type, rval_type)
        rval = cast_expr(rval, rval_type, lval_type)
        if operand == "==":
            expr = lval == rval
        elif operand == "!=":
            expr = lval != rval
        elif operand == "<":
            expr = lval < rval
        elif operand == ">":
            expr = lval > rval
        elif operand == "<=":
            expr = lval <= rval
        elif operand == ">=":
            expr = lval >= rval
        elif operand == "is":
            lval, _ = _select_value(lhs, session)
            expr = lval.is_(rval)
        elif operand == "is not":
            expr = lval.isnot(rval)
        return _join_subqueries(lhs, rhs, expr, "bool", session=session)
    elif lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)

        # Special handling for JSONB array comparison with Python list literals
        if (
            operand in ("==", "!=")
            and lval_type in ("list", "dict")
            and isinstance(rval, (list, dict))
        ):
            # Convert Python list/dict to JSONB using json.dumps and cast
            rhs_jsonb = cast(literal(json.dumps(rval)), JSONB)
            if operand == "==":
                expr = lval == rhs_jsonb
            else:
                expr = lval != rhs_jsonb
        else:
            # Standard handling for other types
            lval = cast_expr(lval, lval_type, rval_type)
            rhs = cast_expr(rhs, rval_type, lval_type)
            if operand == "==":
                expr = lval == rhs
            elif operand == "!=":
                expr = lval != rhs
            elif operand == "<":
                expr = lval < rhs
            elif operand == ">":
                expr = lval > rhs
            elif operand == "<=":
                expr = lval <= rhs
            elif operand == ">=":
                expr = lval >= rhs
            elif operand == "is":
                expr = lval.is_(rval)
            elif operand == "is not":
                expr = lval.isnot(rval)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()
    elif rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        rval = cast_expr(rval, rval_type, lval_type)
        lhs = cast_expr(lhs, lval_type, rval_type)
        if operand == "==":
            expr = lhs == rval
        elif operand == "!=":
            expr = lhs != rval
        elif operand == "<":
            expr = lhs < rval
        elif operand == ">":
            expr = lhs > rval
        elif operand == "<=":
            expr = lhs <= rval
        elif operand == ">=":
            expr = lhs >= rval
        elif operand == "is":
            expr = rval.is_(lhs)
        elif operand == "is not":
            expr = rval.isnot(lhs)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("bool").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()
    else:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        rval = cast_expr(rval, rval_type, lval_type)
        lval = cast_expr(lval, lval_type, rval_type)
        if operand == "==":
            return lval == rval
        elif operand == "!=":
            return lval != rval
        elif operand == "<":
            return lval < rval
        elif operand == ">":
            return lval > rval
        elif operand == "<=":
            return lval <= rval
        elif operand == ">=":
            return lval >= rval
        elif operand == "is":
            return lval.is_(rval)
        elif operand == "is not":
            return lval.isnot(rval)


# Helper function for membership operators (in, not in)
def _handle_membership_operator(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
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
                return select(*select_cols).select_from(lhs).subquery()

        # Fall back to standard handling for non-array types
        rhs_list = _parse_rhs_list_or_dict_if_needed(filter_dict.get("rhs"), rhs)

        if rhs_list and isinstance(rhs_list, list):
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
        return select(*select_cols).select_from(lhs).subquery()

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
                return select(*select_cols).select_from(rhs).subquery()

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
        return select(*select_cols).select_from(rhs).subquery()

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
    )
    rhs_expr = build_sql_query(
        rhs_node,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    if isinstance(lhs_expr, Subquery):
        input_type = session.execute(select(lhs_expr.c.inferred_type)).first()[0]
        is_collection = input_type in ["list", "dict"]
        lhs_valcol, lhs_type = _select_value(
            lhs_expr,
            session,
            is_collection=is_collection,
        )
        if isinstance(rhs_expr, Subquery):
            rhs_valcol, rhs_type = _select_value(rhs_expr, session)
            select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend(
                [
                    func.jsonb_extract_path(
                        lhs_valcol,
                        func.cast(rhs_valcol, String),
                    ).label("value"),
                    literal(rhs_type).label("inferred_type"),
                ],
            )
            return (
                select(*select_cols)
                .select_from(lhs_expr)
                .join(rhs_expr, lhs_expr.c.log_event_id == rhs_expr.c.log_event_id)
                .subquery()
            )
        else:
            rhs_expr = (
                rhs_expr.value if isinstance(rhs_expr, BindParameter) else rhs_expr
            )
            if lhs_type == "str":
                # For strings, we need to use PostgreSQL's substring function
                # PostgreSQL is 1-indexed, so we need to adjust the index
                if isinstance(rhs_expr, int):
                    # Convert 0-indexed to 1-indexed for PostgreSQL
                    pg_index = rhs_expr + 1
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        literal(pg_index),
                        literal(1),
                    )
                elif isinstance(rhs_expr, BindParameter) and isinstance(
                    rhs_expr.value,
                    int,
                ):
                    # Convert 0-indexed to 1-indexed for PostgreSQL
                    pg_index = rhs_expr.value + 1
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        literal(pg_index),
                        literal(1),
                    )
                else:
                    # If it's not a simple integer index, try to cast it
                    extracted = func.substring(
                        func.replace(cast(lhs_valcol, String), '"', ""),
                        cast(rhs_expr, Integer) + 1,
                        literal(1),
                    )
            # Standard JSONB indexing for non-string types
            elif isinstance(rhs_expr, int):
                extracted = lhs_valcol[rhs_expr]  # Postgres list indexing
            elif isinstance(rhs_expr, str):
                extracted = lhs_valcol[rhs_expr]  # Postgres dict indexing
            else:
                # fallback
                extracted = lhs_valcol[rhs_expr]

            result = session.execute(select(extracted)).first()[0]
            inferred_type = LogDAO.infer_type("", result)
            select_cols = [lhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in lhs_expr.c.keys():
                select_cols.append(lhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend(
                [
                    extracted.label("value"),
                    literal(inferred_type).label("inferred_type"),
                ],
            )
            return select(*select_cols).select_from(lhs_expr).subquery()

    else:
        # If LHS is not a subquery => e.g. LHS is a python dict or list literal
        if isinstance(lhs_expr, (dict, list)):
            # Then we do a python-level extraction if the rhs is also python-literal
            if isinstance(rhs_expr, (int, str)):
                # Just do dictionary or list indexing:
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

        return select(*select_cols).select_from(lhs_expr).subquery()
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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    # Both sides subqueries
    if lhs_is_sub and rhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        expr = lval.op("<->")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, expr, "float", session=session)

    # Only LHS is subquery
    if lhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        expr = lval.op("<->")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    # Only RHS is subquery
    if rhs_is_sub:
        rval, _ = _select_value(rhs, session)
        lval, _ = _select_value(lhs, session)
        expr = lval.op("<->")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    # Neither side subquery
    return lhs.op("<->")(rhs).cast(Float)


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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        dist = lval.op("<=>")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, dist, "float", session=session)

    if lhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        dist = lval.op("<=>")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [dist.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    if rhs_is_sub:
        rval, _ = _select_value(rhs, session)
        lval, _ = _select_value(lhs, session)
        dist = lval.op("<=>")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [dist.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    dist = lhs.op("<=>")(rhs).cast(Float)
    return dist


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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<#>")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, expr, "float", session=session)

    if lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<#>")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    if rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr = lval.op("<#>")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    return lhs.op("<#>")(rhs).cast(Float)


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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<+>")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, expr, "float", session=session)

    if lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<+>")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    if rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr = lval.op("<+>")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    return lhs.op("<+>")(rhs).cast(Float)


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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<~>")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, expr, "float", session=session)

    if lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<~>")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    if rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr = lval.op("<~>")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    return lhs.op("<~>")(rhs).cast(Float)


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
    )
    rhs = build_sql_query(
        filter_dict.get("rhs"),
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<%>")(rval).cast(Float)
        return _join_subqueries(lhs, rhs, expr, "float", session=session)

    if lhs_is_sub:
        lval, lval_type = _select_value(lhs, session)
        rval, rval_type = _select_value(rhs, session)
        expr = lval.op("<%>")(rval).cast(Float)
        select_cols = [lhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in lhs.c.keys():
            select_cols.append(lhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(lhs).subquery()

    if rhs_is_sub:
        rval, rval_type = _select_value(rhs, session)
        lval, lval_type = _select_value(lhs, session)
        expr = lval.op("<%>")(rval).cast(Float)
        select_cols = [rhs.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs.c.keys():
            select_cols.append(rhs.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("float").label("inferred_type")],
        )
        return select(*select_cols).select_from(rhs).subquery()

    return lhs.op("<%>")(rhs).cast(Float)
