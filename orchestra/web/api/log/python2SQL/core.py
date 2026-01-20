from sqlalchemy import literal, select
from sqlalchemy.sql.selectable import Subquery

from . import alias_utils, jsonb_builder

__all__ = ["build_sql_query", "_compute_expression"]


def _compute_expression(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids=None,
    project_id=None,
    context_id=None,
):
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
        project_id=project_id,
        context_id=context_id,
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
    project_id=None,
    context_id=None,
    enable_cte_optimization=False,
    query_context=None,
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
        project_id: The project ID, required for JSONB field type lookup.
        context_id: The context ID, optional for JSONB field type lookup.
        enable_cte_optimization: Whether to enable CTE-based aggregation optimization.
        query_context: Optional QueryContext for tracking CTEs during query building.

    Returns:
        SQLAlchemy condition or expression.
        If enable_cte_optimization is True and CTEs were generated, returns a tuple
        of (expression, query_context).
    """

    if local_scope is None:
        local_scope = {}

    # Create QueryContext for CTE optimization if enabled and not already provided
    if enable_cte_optimization and query_context is None:
        from .query_context import QueryContext

        query_context = QueryContext()

    result = _build_sql_query(
        filter_dict,
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
        query_context=query_context,
    )

    # Return tuple with query_context if CTEs were generated
    if query_context is not None and query_context.has_aggregations():
        return (result, query_context)
    return result


# NOTE: ~270 lines of deprecated code were removed from here
# The _build_sql_query function now calls _build_sql_query directly


def _build_sql_query(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    *,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Build SQLAlchemy filter expressions using JSONB column operations.

    Args:
        query_context: Optional QueryContext for tracking CTEs during query building.
                       When provided, aggregation functions can register CTEs for
                       pre-computation instead of using correlated subqueries.
    """
    if local_scope is None:
        local_scope = {}

    if not isinstance(filter_dict, dict):
        # Handle list literals by casting to JSONB to avoid Postgres array inference issues
        if isinstance(filter_dict, list):
            from sqlalchemy.dialects.postgresql import JSONB

            # Check if list contains special dicts (type_literal, identifier, etc.)
            # If so, process each element recursively
            has_special_dicts = any(
                isinstance(item, dict)
                and item.get("type")
                in ("type_literal", "identifier", "list", "literal_list")
                for item in filter_dict
            )
            if has_special_dicts:
                # Build each element and return as Python list of SQL expressions
                return [
                    _build_sql_query(
                        item,
                        log_event_alias,
                        session,
                        log_event_ids,
                        is_derived=is_derived,
                        local_scope=local_scope,
                        is_vector=is_vector,
                        project_id=project_id,
                        context_id=context_id,
                        query_context=query_context,
                    )
                    for item in filter_dict
                ]

            # Pass list directly, let SQLAlchemy/Psycopg2 handle JSONB serialization
            return literal(filter_dict, type_=JSONB)
        return literal(filter_dict)

    if "type" in filter_dict:
        if filter_dict["type"] == "list":
            import json

            from sqlalchemy.dialects.postgresql import JSONB

            # Use json.dumps to create a JSON string, then cast to JSONB
            # This avoids Postgres trying to interpret it as a native array (which fails for mixed types)
            return literal(json.dumps(filter_dict["value"]), type_=JSONB)
        if filter_dict["type"] == "identifier":
            key = filter_dict["value"]

            # When identifier is in local_scope (from comprehensions), return stored expression
            if key in local_scope:
                col, itype = local_scope[key]
                # Check if we need to wrap in a subquery for comprehension context
                base_sub = local_scope.get("__comp_base__", {}).get(key)
                if base_sub is not None and "__comp_idx__" in local_scope:
                    # Build subquery with comprehension index
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

                # For simple expressions, return directly
                from sqlalchemy.sql.selectable import ColumnClause, Subquery

                if isinstance(col, (Subquery, ColumnClause)):
                    return col
                # Return the expression directly for JSONB mode
                # Annotate the expression with type info so aggregation functions can use it
                if hasattr(col, "_annotate"):
                    col = col._annotate({"inferred_type": itype})
                return col

            if isinstance(log_event_ids, dict):
                event_ids = log_event_ids.get(key)
            else:
                event_ids = log_event_ids

            return jsonb_builder._build_jsonb_field_expression(
                key,
                log_event_alias,
                session,
                log_event_ids=event_ids,
                is_derived=is_derived,
                is_vector=is_vector,
                project_id=project_id,
                context_id=context_id,
            )
        elif filter_dict["type"] == "type_literal":
            return literal(filter_dict["value"])
        elif filter_dict["type"] in ("int", "float", "bool", "string", "other"):
            return literal(filter_dict["value"])

    operand = filter_dict.get("operand")

    # List comprehensions: [x*2 for x in scores if x > 0]
    if operand == "list_comp":
        return jsonb_builder._handle_list_comp_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Dictionary comprehensions: {k: v*2 for k, v in items.items()}
    if operand == "dict_comp":
        return jsonb_builder._handle_dict_comp_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Conditional expressions: x if condition else y
    if operand == "if_expr":
        return jsonb_builder._handle_if_expr_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Index operator: arr[0], dict["key"]
    if operand == "INDEX":
        return jsonb_builder._handle_index_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Slice operator: arr[1:5], str[:10]
    if operand == "SLICE":
        return jsonb_builder._handle_slice_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Dictionary methods: .keys(), .values(), .items(), .get()
    if operand == "dict_method":
        return jsonb_builder._handle_dict_method_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # String methods: .lower(), .upper(), .strip(), .startswith(), etc.
    if operand == "str_method":
        return jsonb_builder._handle_str_method_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Core operators

    if operand in ("and", "or", "not"):
        return jsonb_builder._handle_logical_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )
    elif operand in ("+", "-", "*", "/", "%", "**", "//"):
        return jsonb_builder._handle_arithmetic_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return jsonb_builder._handle_comparison_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )
    elif operand in ("in", "not in"):
        return jsonb_builder._handle_membership_operator_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Function handlers

    if operand in [
        # Type casts - all properly handle Subquery operands
        "int",
        "float",
        "str",
        "bool",
        # Other functions
        "len",
        "type",
        "round",
        "round_timestamp",
        "exists",
        "version",
        "isNone",
        "time",
        "date",
        "now",
        "num_tokens",
        "mean",
        "sum",
        "var",
        "std",
        "min",
        "max",
        "median",
        "mode",
        "embed",
        "embed_image",
        "phash",
        "BASE",
        "jsonb_build_array",
        "jsonb_build_object",
    ]:
        return jsonb_builder._handle_functions_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Vector operations

    if operand in ("l2", "euclidean_distance"):
        return jsonb_builder._handle_l2_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    if operand == "cosine":
        return jsonb_builder._handle_cosine_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    if operand == "ip":
        return jsonb_builder._handle_ip_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    if operand == "l1":
        return jsonb_builder._handle_l1_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    if operand == "hamming":
        # Hamming distance uses the same vector binary op with <~> operator
        lhs = _build_sql_query(
            filter_dict.get("lhs"),
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=True,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )
        rhs = _build_sql_query(
            filter_dict.get("rhs"),
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=True,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )
        return jsonb_builder._vector_binary_op_jsonb(
            lhs,
            rhs,
            session,
            "<~>",
            "float",
            "hamming_distance",
        )

    if operand == "jaccard":
        return jsonb_builder._handle_jaccard_distance_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    if operand == "phash_distance":
        return jsonb_builder._handle_phash_distance_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # zip function - handle locally for JSONB
    if operand == "zip":
        return jsonb_builder._handle_zip_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
            query_context=query_context,
        )

    # Fallback: Handle plain dict/value literals (operand is None)

    if operand is None:
        from sqlalchemy.dialects.postgresql import JSONB

        # Cast plain dicts to JSONB for proper comparison
        return literal(filter_dict, type_=JSONB)

    raise NotImplementedError(
        f"JSONB support for operand '{operand}' not yet implemented",
    )
