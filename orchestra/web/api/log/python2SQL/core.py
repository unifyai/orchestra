import re

from sqlalchemy import literal, select
from sqlalchemy.sql.selectable import ColumnClause, Subquery

from . import alias_utils
from .helpers import _build_subquery_for_identifier

__all__ = ["build_sql_query", "_compute_expression"]


def _compute_expression(filter_dict, log_event_alias, session, log_event_ids=None):
    """
    Use build_sql_query -> subquery or expression -> .execute() -> return single result.
    If multiple rows, pick the first or do an aggregator as needed.
    """
    expr = build_sql_query(
        filter_dict,
        log_event_alias,
        session,
        log_event_ids=log_event_ids,
        is_derived=True,
    )
    if isinstance(expr, Subquery):
        rows = session.execute(select(expr.c.log_event_id, expr.c.value)).fetchall()
        if not rows:
            return None
        return rows
    else:
        return session.execute(select(expr)).scalar()


def build_sql_query(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    *,
    local_scope=None,
    is_vector=False,
):
    """
    Recursively build SQLAlchemy filter or expression from filter_dict.

    Args:
        filter_dict (dict): The filter dictionary.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.
        log_event_ids: IDs of log events to filter on.
        is_derived: Whether this is for a derived log.
        local_scope: Dictionary mapping local variable names to (column, type) tuples.
        Used for comprehensions to avoid building subqueries for local variables.
        is_vector: Whether to treat the result as a vector type.

    Returns:
        SQLAlchemy condition or expression
    """
    from .functions import (
        _handle_dict_comp,
        _handle_dict_method,
        _handle_functions,
        _handle_if_expr,
        _handle_list_comp,
        _handle_zip,
    )
    from .operators import (
        _handle_arithmetic_operator,
        _handle_comparison_operator,
        _handle_cosine,
        _handle_hamming,
        _handle_index_operator,
        _handle_ip,
        _handle_jaccard,
        _handle_l1,
        _handle_l2,
        _handle_logical_operator,
        _handle_membership_operator,
        _handle_phash_distance,
        _handle_slice_operator,
    )

    if local_scope is None:
        local_scope = {}

    if not isinstance(filter_dict, dict):
        return literal(filter_dict)

    if "type" in filter_dict:
        if filter_dict["type"] == "identifier":
            key = filter_dict["value"]

            if key in local_scope:
                col, itype = local_scope[key]

                base_sub = local_scope.get("__comp_base__", {}).get(key)
                if base_sub is not None and "__comp_idx__" in local_scope:
                    cols = [
                        base_sub.c.log_event_id.label("log_event_id"),
                        base_sub.c.ordinality.label("__comp_idx__"),
                        col.label("value"),
                        literal(itype).label("inferred_type"),
                    ]
                    if "__parent_idx__" in local_scope and hasattr(
                        base_sub.c,
                        "__parent_idx__",
                    ):
                        cols.append(base_sub.c.__parent_idx__.label("__parent_idx__"))

                    subq = (
                        select(*cols)
                        .select_from(base_sub)
                        .subquery(name=alias_utils.unique_alias(f"__local_{key}"))
                    )
                    return subq

                if isinstance(col, (Subquery, ColumnClause)):
                    return col

            if isinstance(log_event_ids, dict):
                event_ids = log_event_ids.get(key)
            else:
                event_ids = log_event_ids

            # Sanitize the key for use in alias to ensure it's a valid SQL identifier
            safe_key = re.sub(r"[^a-zA-Z0-9_]", "_", str(key))
            if not safe_key:
                safe_key = "key"

            return _build_subquery_for_identifier(
                key,
                log_event_alias,
                alias=f"select_{safe_key}",
                log_event_ids=event_ids,
                session=session,
                is_derived=is_derived,
                is_vector=is_vector,
            )
        elif filter_dict["type"] == "type_literal":
            return literal(filter_dict["value"])
        elif filter_dict["type"] in ("int", "float", "bool", "string", "other"):
            return literal(filter_dict["value"])
    operand = filter_dict.get("operand")

    if operand in ("and", "or", "not"):
        return _handle_logical_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("+", "-", "*", "/", "%", "**", "//"):
        return _handle_arithmetic_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return _handle_comparison_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("in", "not in"):
        return _handle_membership_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in (
        "len",
        "str",
        "type",
        "round",
        "round_timestamp",
        "num_tokens",
        "exists",
        "version",
        "BASE",
        "isNone",
        "time",
        "date",
        "now",
        "mean",
        "sum",
        "var",
        "std",
        "min",
        "max",
        "median",
        "mode",
        "embed",
        "phash",
    ):
        return _handle_functions(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "INDEX":
        return _handle_index_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "SLICE":
        return _handle_slice_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "dict_method":
        return _handle_dict_method(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "if_expr":
        return _handle_if_expr(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "list_comp":
        return _handle_list_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "dict_comp":
        return _handle_dict_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "zip":
        return _handle_zip(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "l2":
        return _handle_l2(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "cosine":
        return _handle_cosine(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "ip":
        return _handle_ip(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "l1":
        return _handle_l1(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "hamming":
        return _handle_hamming(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "jaccard":
        return _handle_jaccard(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "phash_distance":
        return _handle_phash_distance(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "str_method":
        from .functions import _handle_str_method

        return _handle_str_method(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    else:
        if operand is not None:
            raise ValueError(f"Unknown operand or structure: {filter_dict}")
        else:
            return literal(filter_dict)
