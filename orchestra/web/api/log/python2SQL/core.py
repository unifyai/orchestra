import re

from sqlalchemy import literal, select
from sqlalchemy.sql.selectable import ColumnClause, Subquery

from orchestra.settings import settings

from . import alias_utils, jsonb_builder
from .helpers import _build_subquery_for_identifier

__all__ = ["build_sql_query", "_compute_expression"]


async def _compute_expression(
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
    expr = await build_sql_query(
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


async def build_sql_query(
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

    # Route to appropriate query builder based on feature flag
    if settings.use_jsonb_queries:
        # Create QueryContext for CTE optimization if enabled and not already provided
        if enable_cte_optimization and query_context is None:
            from .query_context import QueryContext

            query_context = QueryContext()

        result = await _build_sql_query_jsonb(
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
        return await _handle_logical_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("+", "-", "*", "/", "%", "**", "//"):
        return await _handle_arithmetic_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("==", "!=", "<", ">", "<=", ">=", "is", "is not"):
        return await _handle_comparison_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand in ("in", "not in"):
        return await _handle_membership_operator(
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
        "embed_image",
        "phash",
    ):
        return await _handle_functions(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
    elif operand == "INDEX":
        return await _handle_index_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "SLICE":
        return await _handle_slice_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "dict_method":
        return await _handle_dict_method(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
    elif operand == "if_expr":
        return await _handle_if_expr(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "list_comp":
        return await _handle_list_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
    elif operand == "dict_comp":
        return await _handle_dict_comp(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
    elif operand == "zip":
        return await _handle_zip(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
    elif operand == "l2":
        return await _handle_l2(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "cosine":
        return await _handle_cosine(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "ip":
        return await _handle_ip(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "l1":
        return await _handle_l1(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "hamming":
        return await _handle_hamming(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "jaccard":
        return await _handle_jaccard(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "phash_distance":
        return await _handle_phash_distance(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
        )
    elif operand == "str_method":
        from .functions import _handle_str_method

        return await _handle_str_method(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
    else:
        if operand is not None:
            raise ValueError(f"Unknown operand or structure: {filter_dict}")
        else:
            return literal(filter_dict)


async def _build_sql_query_jsonb(
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
    import asyncio

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
                # Build each element concurrently and return as Python list of SQL expressions
                results = await asyncio.gather(
                    *[
                        _build_sql_query_jsonb(
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
                    ],
                )
                return list(results)

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
        return await jsonb_builder._handle_list_comp_jsonb(
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
        return await jsonb_builder._handle_dict_comp_jsonb(
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
        return await jsonb_builder._handle_if_expr_jsonb(
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
        return await jsonb_builder._handle_index_operator_jsonb(
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
        return await jsonb_builder._handle_slice_operator_jsonb(
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
        return await jsonb_builder._handle_dict_method_jsonb(
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
        return await jsonb_builder._handle_str_method_jsonb(
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
        return await jsonb_builder._handle_logical_operator_jsonb(
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
        return await jsonb_builder._handle_arithmetic_operator_jsonb(
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
        return await jsonb_builder._handle_comparison_operator_jsonb(
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
        return await jsonb_builder._handle_membership_operator_jsonb(
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
        return await jsonb_builder._handle_functions_jsonb(
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
        return await jsonb_builder._handle_l2_jsonb(
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
        return await jsonb_builder._handle_cosine_jsonb(
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
        return await jsonb_builder._handle_ip_jsonb(
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
        return await jsonb_builder._handle_l1_jsonb(
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
        lhs = await _build_sql_query_jsonb(
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
        rhs = await _build_sql_query_jsonb(
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
        return await jsonb_builder._handle_jaccard_distance_jsonb(
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
        return await jsonb_builder._handle_phash_distance_jsonb(
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
        return await jsonb_builder._handle_zip_jsonb(
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
