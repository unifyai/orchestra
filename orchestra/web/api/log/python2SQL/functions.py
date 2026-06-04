from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import (
    BindParameter,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    and_,
    case,
    cast,
    func,
    lateral,
    literal,
    select,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.sql.selectable import ColumnClause, Subquery

from orchestra.db.dao.log_event_dao import LogEventDAO

from . import alias_utils, jsonb_builder
from .core import build_sql_query
from .helpers import (
    _get_parent_idx,
    _is_jsonb_expression,
    _select_value,
    cast_expr,
    unify_inferred_types,
)

__all__ = [
    "_handle_functions",
    "_handle_dict_method",
    "_handle_if_expr",
    "_handle_list_comp",
    "_handle_dict_comp",
    "_handle_zip",
    "_handle_dict_get",
    "_handle_str_method",
]


# Helper function for functions (len, str, type, round, round_timestamp, exists, version, isNone)
def _handle_date_function(rhs_expr, session):
    """
    Handles the date() function which extracts the date component from a datetime value.

    Args:
        rhs_expr: The expression to extract the date from (datetime or string)
        session: SQLAlchemy session for executing subqueries

    Returns:
        SQLAlchemy expression that extracts the date component
    """
    if isinstance(rhs_expr, Subquery):
        val, val_type = _select_value(rhs_expr, session)

        # Create a CASE expression to handle different input types
        expr = case(
            (
                val_type == "datetime",
                func.cast(
                    func.date_trunc(
                        "day",
                        cast(cast(val, Text), DateTime(timezone=True)),
                    ),
                    Date,
                ),
            ),
            (val_type == "str", func.cast(cast(val, Text), Date)),
            else_=None,
        )
        if isinstance(rhs_expr, ColumnClause):
            return expr
        select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in rhs_expr.c.keys():
            select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in rhs_expr.c.keys():
            select_cols.append(rhs_expr.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal("date").label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(rhs_expr),
            prefix="func_result",
        )
    else:
        # Handle literal values
        if isinstance(rhs_expr, BindParameter):
            val = rhs_expr.value
            if isinstance(val, datetime):
                # Extract date from datetime
                return literal(val.date().isoformat(), type_=Date)
            elif isinstance(val, str):
                # Try to parse as datetime first
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return literal(dt.date().isoformat(), type_=Date)
                except ValueError:
                    # If it's already a date string, just pass it as is
                    from orchestra.web.api.log.utils.type_utils import _is_date_string

                    if _is_date_string(val):
                        clean_val = val.strip("\"'")
                        return literal(clean_val, type_=Date)
                    else:
                        raise ValueError(
                            f"Cannot convert {val} to date. Expected datetime or date string.",
                        )
            else:
                raise ValueError(
                    f"Cannot convert {val} to date. Expected datetime or date string.",
                )
        else:
            # Try to cast the expression to Date
            return cast(rhs_expr, Date)


def _handle_functions(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
):
    """
    Handles function-based operations ('len', 'str', 'type', 'round', 'round_timestamp',
    'exists', 'version', 'isNone', 'time', 'date', 'now', 'mean', 'sum', 'var', 'std',
    'min', 'max', 'median', 'mode', 'embed', 'duration', 'duration_seconds') in the
    filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the function and its arguments.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.
        project_id: The project ID, required for JSONB field type lookup.
        context_id: The context ID, optional for JSONB field type lookup.

    Returns:
        SQLAlchemy condition or expression based on the provided function.
    """
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
    )


def _handle_dict_method(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle dictionary method calls (.keys(), .values(), .items(), .get()).
    Supports both direct expressions and pre-built subqueries.
    """
    method = filter_dict[
        "method"
    ]  # e.g., "keys", "values", "items", "get", "setdefault"
    if method in ("get", "setdefault"):
        return _handle_dict_get(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope,
            default_supplied=filter_dict.get("default_supplied", False),
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )

    # Check for pre-built src subquery from JSONB wrapper
    # This supports wrapped JSONB expressions and existing subqueries
    if filter_dict.get("_jsonb_src_subq") is not None:
        src = filter_dict["_jsonb_src_subq"]
    else:
        src = build_sql_query(
            filter_dict["rhs"],
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )

        # If src is a JSONB expression, wrap it as a subquery
        if _is_jsonb_expression(src):
            inferred_type = jsonb_builder._infer_expression_type(
                src,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            src = jsonb_builder._wrap_expression_as_subquery(
                src,
                inferred_type,
                log_event_alias,
                session,
                local_scope=local_scope,
                prefix="dict_method_src_wrapped",
            )

    if not isinstance(src, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict.keys/values/items requires a subquery (JSONB expressions are auto-wrapped by the caller)",
        )
    # Extract JSONB column and use lateral join
    val, _ = _select_value(src, session, is_collection=True)

    # Ensure we're working with a JSON object, not an array or scalar
    is_object = func.jsonb_typeof(val) == "object"

    # Use a CASE expression to handle non-object values safely
    safe_val = case((is_object, val), else_=literal("{}", type_=JSONB))

    each = lateral(func.jsonb_each(safe_val).table_valued("key", "value")).alias(
        "each_values",
    )
    parent_idx_col = _get_parent_idx(src.c)
    base_cols = [src.c.log_event_id, each.c.key, each.c.value]
    if parent_idx_col is not None:
        base_cols.append(parent_idx_col.label("__parent_idx__"))

    base = alias_utils.subquery_with_unique_alias(
        select(*base_cols).select_from(src.join(each, true())),
        prefix="base",
    )

    if method == "keys":
        agg = func.coalesce(
            func.jsonb_agg(base.c.key),
            literal("[]", type_=JSONB),
        )
        inf = "list"
    elif method == "values":
        agg = func.coalesce(
            func.jsonb_agg(base.c.value),
            literal("[]", type_=JSONB),
        )
        inf = "list"
    else:  # items
        agg = func.coalesce(
            func.jsonb_agg(
                func.jsonb_build_array(
                    base.c.key,
                    base.c.value,
                ),
            ),
            literal("[]", type_=JSONB),
        )
        inf = "list"

    select_cols = [
        base.c.log_event_id,
        func.coalesce(agg, literal("[]", type_=JSONB)).label("value"),
        literal(inf).label("inferred_type"),
    ]
    group_cols = [base.c.log_event_id]

    if "__parent_idx__" in base.c.keys():
        select_cols.insert(1, base.c.__parent_idx__.label("__parent_idx__"))
        group_cols.append(base.c.__parent_idx__)

    final = alias_utils.subquery_with_unique_alias(
        select(*select_cols).group_by(*group_cols),
        prefix=f"dict_{method}_subquery",
    )
    return final


def _handle_if_expr(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle conditional expressions (ternary if-else) in filter queries.

    This function processes expressions like 'x if condition else y' by evaluating
    the condition and then selecting either the 'then' or 'else' branch accordingly.
    """

    def _inflate_scalar_or_subquery(
        value,
        inferred_type,
        ids_subq,
        has_comp_idx=False,
        local_scope=None,
    ):
        """
        Given a scalar (possibly from a Python literal or BindParameter),
        or an identifier subquery, produce a subquery of the form:

            SELECT
                ids_subq.log_event_id,
                [ids_subq.__comp_idx__],
                [ids_subq.__parent_idx__],
                :value AS value,
                :type  AS inferred_type
            FROM ids_subq

        so we can join on (log_event_id, __comp_idx__) if needed.
        """
        if isinstance(value, Subquery):
            cols = [value.c.log_event_id]
            if hasattr(ids_subq.c, "__comp_idx__"):
                cols.append(ids_subq.c.__comp_idx__)
            elif local_scope and "__comp_idx__" in local_scope:
                idx_col = local_scope["__comp_idx__"][0]
                cols.append(idx_col.label("__comp_idx__"))
            if hasattr(ids_subq.c, "__parent_idx__"):
                cols.append(ids_subq.c.__parent_idx__)
            elif local_scope and "__parent_idx__" in local_scope:
                par_col = local_scope["__parent_idx__"][0]
                cols.append(par_col.label("__parent_idx__"))
            val, inf = _select_value(value, session)
            cols.append(val.label("value"))
            cols.append(literal(inf).label("inferred_type"))
            subq = select(*cols).select_from(value)
            return alias_utils.subquery_with_unique_alias(
                subq,
                prefix=f"inflate_{value.name}",
            )

        if isinstance(value, BindParameter):
            value = value.value

        cols = [ids_subq.c.log_event_id]

        if has_comp_idx:
            if hasattr(ids_subq.c, "__comp_idx__"):
                cols.append(ids_subq.c.__comp_idx__)
            elif local_scope and "__comp_idx__" in local_scope:
                idx_col = local_scope["__comp_idx__"][0]
                cols.append(idx_col.label("__comp_idx__"))
        if hasattr(ids_subq.c, "__parent_idx__"):
            cols.append(ids_subq.c.__parent_idx__)
        elif local_scope and "__parent_idx__" in local_scope:
            par_col = local_scope["__parent_idx__"][0]
            cols.append(par_col.label("__parent_idx__"))

        cols.append(literal(value).label("value"))
        cols.append(literal(inferred_type).label("inferred_type"))

        subq = select(*cols).select_from(ids_subq)
        return alias_utils.subquery_with_unique_alias(
            subq,
            prefix=f"__inflate_scalar_subq_{value}",
        )

    in_comprehension = local_scope is not None and ("__comp_idx__" in local_scope)
    raw_test = build_sql_query(
        filter_dict["test"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )
    raw_body = build_sql_query(
        filter_dict["body"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )
    raw_else = build_sql_query(
        filter_dict["orelse"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )

    id_selects = []
    for part in (raw_test, raw_body, raw_else):
        if isinstance(part, Subquery):
            if in_comprehension and hasattr(part.c, "__comp_idx__"):
                select_cols = [part.c.log_event_id, part.c.__comp_idx__]
                if hasattr(part.c, "__parent_idx__"):
                    select_cols.append(part.c.__parent_idx__)
                id_selects.append(select(*select_cols))
            else:
                id_selects.append(select(part.c.log_event_id))

    if id_selects:
        if in_comprehension and any(len(s.selected_columns) > 1 for s in id_selects):
            standardized_selects = []
            for s in id_selects:
                if len(s.selected_columns) == 1:
                    standardized_selects.append(
                        select(
                            s.selected_columns[0],
                            literal(None).label("__comp_idx__"),
                            literal(None).label("__parent_idx__"),
                        ),
                    )
                else:
                    standardized_selects.append(s)
            union_subq = alias_utils.subquery_with_unique_alias(
                union_all(*standardized_selects),
                prefix="union_all_standardized_selects",
            )
            ids_subq = alias_utils.subquery_with_unique_alias(
                union_subq.select().distinct(),
                prefix="ids_subq",
            )
        else:
            union_subq = alias_utils.subquery_with_unique_alias(
                union_all(*id_selects),
                prefix="union_all_id_selects",
            )
            ids_subq = alias_utils.subquery_with_unique_alias(
                union_subq.select().distinct(),
                prefix="ids_subq",
            )
    else:
        if isinstance(log_event_ids, Subquery):
            ids_subq = alias_utils.subquery_with_unique_alias(
                select(log_event_ids.c.id.label("log_event_id")),
                prefix="ids_subq",
            )
        elif isinstance(log_event_ids, (list, tuple)):
            ids_subq = alias_utils.subquery_with_unique_alias(
                select(literal(id_).label("log_event_id") for id_ in log_event_ids),
                prefix="ids_subq",
            )
        else:
            ids_subq = alias_utils.subquery_with_unique_alias(
                select(log_event_alias.id.label("log_event_id")),
                prefix="ids_subq",
            )

        if in_comprehension:
            comp_idx_col = local_scope["__comp_idx__"][0]
            ids_subq = alias_utils.subquery_with_unique_alias(
                select(
                    ids_subq.c.log_event_id,
                    comp_idx_col.label("__comp_idx__"),
                ).select_from(ids_subq),
                prefix="ids_with_comp_idx",
            )

    if not isinstance(raw_test, Subquery) or (
        isinstance(raw_test, Subquery) and "value" not in raw_test.columns
    ):
        raw_test = _inflate_scalar_or_subquery(
            raw_test,
            (
                "bool"
                if not isinstance(raw_test, BindParameter)
                else LogEventDAO.infer_type("", raw_test.value)
            ),
            ids_subq,
            in_comprehension,
        )

    if not isinstance(raw_body, Subquery) or (
        isinstance(raw_body, Subquery) and "value" not in raw_body.columns
    ):
        raw_body = _inflate_scalar_or_subquery(
            raw_body,
            LogEventDAO.infer_type(
                "",
                raw_body if not isinstance(raw_body, BindParameter) else raw_body.value,
            ),
            ids_subq,
            in_comprehension,
        )

    if not isinstance(raw_else, Subquery) or (
        isinstance(raw_else, Subquery) and "value" not in raw_else.columns
    ):
        raw_else = _inflate_scalar_or_subquery(
            raw_else,
            LogEventDAO.infer_type(
                "",
                raw_else if not isinstance(raw_else, BindParameter) else raw_else.value,
            ),
            ids_subq,
            in_comprehension,
        )

    body_type = session.execute(select(raw_body.c.inferred_type)).scalar()
    else_type = session.execute(select(raw_else.c.inferred_type)).scalar()
    res_type = unify_inferred_types(body_type, else_type)

    body_val = cast_expr(raw_body.c.value, body_type, res_type)
    else_val = cast_expr(raw_else.c.value, else_type, res_type)
    test_val = cast_expr(raw_test.c.value, "bool", "bool")

    case_expr = case(
        (cast(test_val, Boolean), func.to_jsonb(body_val)),
        else_=func.to_jsonb(else_val),
    )

    join_conditions = []

    test_join_cond = ids_subq.c.log_event_id == raw_test.c.log_event_id
    body_join_cond = ids_subq.c.log_event_id == raw_body.c.log_event_id
    else_join_cond = ids_subq.c.log_event_id == raw_else.c.log_event_id

    if in_comprehension:
        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_test.c, "__comp_idx__"):
            test_join_cond = and_(
                test_join_cond,
                ids_subq.c.__comp_idx__ == raw_test.c.__comp_idx__,
            )
        if hasattr(ids_subq.c, "__parent_idx__") and hasattr(
            raw_test.c,
            "__parent_idx__",
        ):
            test_join_cond = and_(
                test_join_cond,
                ids_subq.c.__parent_idx__ == raw_test.c.__parent_idx__,
            )
        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_body.c, "__comp_idx__"):
            body_join_cond = and_(
                body_join_cond,
                ids_subq.c.__comp_idx__ == raw_body.c.__comp_idx__,
            )
        if hasattr(ids_subq.c, "__parent_idx__") and hasattr(
            raw_body.c,
            "__parent_idx__",
        ):
            body_join_cond = and_(
                body_join_cond,
                ids_subq.c.__parent_idx__ == raw_body.c.__parent_idx__,
            )
        if hasattr(ids_subq.c, "__comp_idx__") and hasattr(raw_else.c, "__comp_idx__"):
            else_join_cond = and_(
                else_join_cond,
                ids_subq.c.__comp_idx__ == raw_else.c.__comp_idx__,
            )
        if hasattr(ids_subq.c, "__parent_idx__") and hasattr(
            raw_else.c,
            "__parent_idx__",
        ):
            else_join_cond = and_(
                else_join_cond,
                ids_subq.c.__parent_idx__ == raw_else.c.__parent_idx__,
            )

    select_cols = [ids_subq.c.log_event_id]
    if in_comprehension and hasattr(ids_subq.c, "__comp_idx__"):
        select_cols.append(ids_subq.c.__comp_idx__)
    if in_comprehension and hasattr(ids_subq.c, "__parent_idx__"):
        select_cols.append(ids_subq.c.__parent_idx__)
    select_cols.extend(
        [case_expr.label("value"), literal(res_type).label("inferred_type")],
    )

    # Generate a unique alias to prevent collisions in nested queries
    alias_name = alias_utils.unique_alias("if_expr_subq")

    final_subq = select(*select_cols).select_from(
        ids_subq.join(raw_test, test_join_cond)
        .outerjoin(raw_body, body_join_cond)
        .outerjoin(raw_else, else_join_cond),
    )
    final_subq = alias_utils.subquery_with_unique_alias(
        final_subq,
        prefix="if_expr",
    )

    return final_subq


def _handle_list_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle list comprehension expressions in filter queries.

    This function processes expressions like [x*2 for x in some_list if x > 0]
    by exploding the source list into rows, then applying the transformation and
    filter to each element, and finally aggregating back into a list.
    """
    # Check for pre-built iter subquery from JSONB wrapper
    # This supports wrapped JSONB expressions and existing subqueries
    if filter_dict.get("_jsonb_iter_subq") is not None:
        iter_subq = filter_dict["_jsonb_iter_subq"]
    else:
        iter_subq = build_sql_query(
            filter_dict["iter"],
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )

        # If iter_subq is a JSONB expression, wrap it as a subquery
        if _is_jsonb_expression(iter_subq):
            inferred_type = jsonb_builder._infer_expression_type(
                iter_subq,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            iter_subq = jsonb_builder._wrap_expression_as_subquery(
                iter_subq,
                inferred_type,
                log_event_alias,
                session,
                local_scope=local_scope,
                prefix="list_comp_iter_wrapped",
            )

    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="list comprehension source must be a subquery (JSONB expressions are auto-wrapped by the caller)",
        )

    if not local_scope:
        local_scope = {"__comp_base__": {}}

    val, val_type = _select_value(iter_subq, session, is_collection=True)
    # Fix: Include the subquery in FROM clause when checking type
    # This is necessary because val is a column reference (e.g., zipped.c.value)
    # and we need the subquery in the FROM clause to execute the type check
    is_array = (
        session.execute(
            select(func.jsonb_typeof(val)).select_from(iter_subq).limit(1),
        ).scalar()
        == "array"
    )
    if is_array:
        elem_tbl = (
            func.jsonb_array_elements(val)
            .table_valued("value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )
    else:
        elem_tbl = (
            func.jsonb_each(val)
            .table_valued("key", "value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )

    parent_idx_col = _get_parent_idx(iter_subq.c)
    base_cols = [
        iter_subq.c.log_event_id,
        (elem_tbl.c.value if is_array else elem_tbl.c.value).label("__comp_var__"),
        elem_tbl.c.ordinality,
    ]
    if parent_idx_col is not None:
        base_cols.append(parent_idx_col.label("__parent_idx__"))
    base_stmt = select(*base_cols).select_from(
        iter_subq.outerjoin(elem_tbl, literal(True)),
    )
    base = alias_utils.subquery_with_unique_alias(base_stmt, prefix="base_list_comp")

    unpacking = isinstance(filter_dict["target"], list)
    if unpacking:
        local_scope = {
            "__comp_idx__": (base.c.ordinality, "int"),
            "__comp_base__": {
                **local_scope.pop("__comp_base__"),
                **{
                    ident["value"]: base
                    for i, ident in enumerate(filter_dict["target"])
                },
            },
            **local_scope,
        }
        for i, ident in enumerate(filter_dict["target"]):
            comp_col = func.coalesce(base.c.__comp_var__.op("->")(i), "null")
            # Fix: Include base in FROM clause when executing type inference query
            comp_type = LogEventDAO.infer_type(
                "",
                session.execute(select(comp_col).select_from(base).limit(1)).scalar(),
            )
            local_scope[ident["value"]] = (comp_col, comp_type)
    else:
        # Fix: Include base in FROM clause when executing type inference query
        comp_type = LogEventDAO.infer_type(
            "",
            session.execute(
                select(base.c.__comp_var__).select_from(base).limit(1),
            ).scalar(),
        )
        local_scope = {
            filter_dict["target"]["value"]: (base.c.__comp_var__, comp_type),
            "__comp_idx__": (base.c.ordinality, "int"),
            "__comp_base__": {
                **local_scope.pop("__comp_base__"),
                filter_dict["target"]["value"]: base,
            },
            **local_scope,
        }

    if parent_idx_col is not None:
        local_scope["__parent_idx__"] = (parent_idx_col, "int")
    elt_expr = build_sql_query(
        filter_dict["elt"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )

    def _value_column(expr):
        if isinstance(expr, Subquery):
            has_idx = hasattr(expr.c, "__comp_idx__")
            return (
                expr.c.value,
                expr,
                has_idx,
            )
        return expr, None, False

    elt_col, elt_subq, has_idx = _value_column(elt_expr)

    if elt_subq is not None:
        elt_with_row = select(
            elt_subq.c.log_event_id,
            (elt_subq.c.__comp_idx__ if has_idx else func.row_number().over()).label(
                "ordinality",
            ),
            *(
                [elt_subq.c.__parent_idx__.label("__parent_idx__")]
                if hasattr(elt_subq.c, "__parent_idx__")
                else []
            ),
            elt_subq.c.value,
            elt_subq.c.inferred_type,
        ).select_from(elt_subq)
        elt_with_row = alias_utils.subquery_with_unique_alias(
            elt_with_row,
            prefix="elt_with_row",
        )
        columns = [
            base.c.log_event_id.label("log_event_id"),
            *(
                [base.c.__parent_idx__.label("__parent_idx__")]
                if parent_idx_col is not None
                else []
            ),
            base.c.ordinality.label("ordinality"),
            elt_with_row.c.value.label("value"),
            elt_with_row.c.inferred_type.label("inferred_type"),
        ]
        from_clause = (
            select(*columns)
            .select_from(
                base.outerjoin(
                    elt_with_row,
                    and_(
                        base.c.log_event_id == elt_with_row.c.log_event_id,
                        base.c.ordinality == elt_with_row.c.ordinality,
                        *(
                            [base.c.__parent_idx__ == elt_with_row.c.__parent_idx__]
                            if hasattr(base.c, "__parent_idx__")
                            and hasattr(elt_with_row.c, "__parent_idx__")
                            else []
                        ),
                    ),
                ),
            )
            .order_by(base.c.log_event_id, base.c.ordinality, elt_with_row.c.ordinality)
        )
        from_clause = alias_utils.subquery_with_unique_alias(
            from_clause,
            prefix="from_clause",
        )
        elt_col = from_clause.c.value
    else:
        from_clause = base

    where_clause = literal(True)
    for cond_ast in filter_dict.get("ifs", []):
        cond_expr = build_sql_query(
            cond_ast,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
        if isinstance(cond_expr, Subquery):
            condition = (
                select(cond_expr.c.value)
                .where(
                    cond_expr.c.log_event_id == from_clause.c.log_event_id,
                    cond_expr.c.__comp_idx__ == from_clause.c.ordinality,
                    *(
                        [cond_expr.c.__parent_idx__ == from_clause.c.__parent_idx__]
                        if hasattr(cond_expr.c, "__parent_idx__")
                        and hasattr(from_clause.c, "__parent_idx__")
                        else []
                    ),
                )
                .scalar_subquery()
            )
        else:
            condition = cond_expr
        where_clause = and_(where_clause, condition)

    # Build the final subquery for the list comprehension
    if parent_idx_col is not None:
        # nested comprehension
        select_cols = [
            from_clause.c.log_event_id.label("log_event_id"),
            from_clause.c.__parent_idx__.label("__comp_idx__"),
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(elt_col, from_clause.c.ordinality),
                ).filter(elt_col.isnot(None)),
                literal([], type_=JSONB),
            ).label("value"),
            literal("list").label("inferred_type"),
        ]
        group_by_cols = [
            from_clause.c.log_event_id,
            from_clause.c.__parent_idx__,
        ]
    else:
        # top-level comprehension
        select_cols = [
            from_clause.c.log_event_id,
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(elt_col, from_clause.c.ordinality),
                ).filter(elt_col.isnot(None)),
                literal([], type_=JSONB),
            ).label("value"),
            literal("list").label("inferred_type"),
        ]
        group_by_cols = [
            from_clause.c.log_event_id,
        ]
    final = (
        select(*select_cols)
        .select_from(from_clause)
        .where(where_clause)
        .group_by(*group_by_cols)
    )
    final = alias_utils.subquery_with_unique_alias(
        final,
        prefix="list_comp_final",
    )
    return final


def _handle_str_method(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle string method calls in filter queries.

    Process string method calls by mapping Python methods to PostgreSQL equivalents.
    """
    method = filter_dict[
        "method"
    ]  # e.g., "lower", "upper", "capitalize", "strip", etc.
    bool_methods = {"startswith", "endswith", "contains", "match"}
    inferred = "bool" if method in bool_methods else "str"

    src = build_sql_query(
        filter_dict["rhs"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )

    # Get arguments if any
    args = []
    if "args" in filter_dict and filter_dict["args"]:
        args = [
            build_sql_query(
                arg,
                log_event_alias,
                session,
                log_event_ids,
                is_derived=is_derived,
                local_scope=local_scope,
                is_vector=is_vector,
                project_id=project_id,
                context_id=context_id,
            )
            for arg in filter_dict["args"]
        ]

    # Map Python string methods to PostgreSQL functions
    if isinstance(src, (Subquery, ColumnClause)):
        val, val_type = _select_value(src, session)

        # Ensure we're working with a string
        # Use btrim to strip only SURROUNDING quotes (first/last char), not ALL quotes
        # This preserves internal quotes for JSON arrays like [1, "a"]
        # while still stripping the outer quotes from scalar strings like "TEST"
        str_val = func.btrim(cast(val, String), literal('"'))

        # Apply the appropriate string operation
        if method == "lower":
            expr = func.lower(str_val)
        elif method == "upper":
            expr = func.upper(str_val)
        elif method == "capitalize":
            # First char uppercase, rest lowercase:
            expr = func.concat(
                func.upper(func.substr(str_val, 1, 1)),
                func.lower(func.substr(str_val, 2)),
            )
        elif method == "strip":
            if args:
                chars = cast(args[0], String)
                expr = func.btrim(str_val, chars)
            else:
                expr = func.regexp_replace(str_val, "^\\s+|\\s+$", "", "g")
        elif method == "lstrip":
            if args:
                chars = cast(args[0], String)
                expr = func.ltrim(str_val, chars)
            else:
                expr = func.regexp_replace(str_val, "^\\s+", "", "g")
        elif method == "rstrip":
            if args:
                chars = cast(args[0], String)
                expr = func.rtrim(str_val, chars)
            else:
                expr = func.regexp_replace(str_val, "\\s+$", "", "g")
        elif method == "startswith":
            if not args:
                raise ValueError("startswith() requires a prefix argument")

            prefix = cast(args[0], String)
            # substr(str_val, 1, length(prefix)) = prefix
            expr = func.substr(str_val, 1, func.length(prefix)) == prefix

        elif method == "endswith":
            if not args:
                raise ValueError("endswith() requires a suffix argument")

            suffix = cast(args[0], String)
            expr = func.right(str_val, func.length(suffix)) == suffix
        elif method == "contains":
            if not args:
                raise ValueError("contains() requires a substring argument")
            substring = args[0]
            if isinstance(substring, BindParameter):
                substring_val = substring.value
                expr = func.position(str_val, substring_val) > 0
            else:
                expr = func.position(str_val, substring) > 0
        elif method == "match":
            if not args:
                raise ValueError("match() requires a pattern argument")
            pattern = args[0]
            expr = str_val.op("~")(pattern)
        elif method == "replace":
            if len(args) < 2:
                raise ValueError("replace() requires old and new substring arguments")
            old = args[0]
            new = args[1]
            expr = func.replace(str_val, old, new)
        elif method == "substring" or method == "__getitem__":
            # Handle both substring() and slice notation
            if method == "substring":
                if not args:
                    raise ValueError("substring() requires at least a start argument")
                start = args[0]
                length = args[1] if len(args) > 1 else None

                # PostgreSQL substring is 1-indexed
                if isinstance(start, BindParameter) and isinstance(start.value, int):
                    # Add 1 to convert from 0-indexed Python to 1-indexed PostgreSQL
                    start_val = start.value + 1 if start.value >= 0 else start.value
                    start = literal(start_val)
                else:
                    # For dynamic values, add 1 in the SQL
                    start = cast(start, Integer) + 1

                if length is not None:
                    length = cast(length, Integer)
                    expr = func.substring(str_val, start, length)
                else:
                    expr = func.substring(str_val, start)
            else:  # __getitem__ (slice notation)
                # Handle slice objects
                slice_obj = filter_dict.get("slice", {})
                start = slice_obj.get("start")
                stop = slice_obj.get("stop")

                if start is not None:
                    start = build_sql_query(
                        start,
                        log_event_alias,
                        session,
                        log_event_ids,
                        is_derived=is_derived,
                        local_scope=local_scope,
                        is_vector=is_vector,
                    )
                    # Convert to 1-indexed
                    if isinstance(start, BindParameter) and isinstance(
                        start.value,
                        int,
                    ):
                        start_val = start.value + 1 if start.value >= 0 else start.value
                        start = literal(start_val)
                    else:
                        start = cast(start, Integer) + 1
                else:
                    start = literal(1)  # Default to beginning of string

                if stop is not None:
                    stop = build_sql_query(
                        stop,
                        log_event_alias,
                        session,
                        log_event_ids,
                        is_derived=is_derived,
                        local_scope=local_scope,
                        is_vector=is_vector,
                    )
                    stop = cast(stop, Integer)
                    # Calculate length (stop - start)
                    if (
                        isinstance(stop, BindParameter)
                        and isinstance(stop.value, int)
                        and isinstance(start, BindParameter)
                        and isinstance(start.value, int)
                    ):
                        length = stop.value - start.value
                        expr = func.substring(str_val, start, literal(length))
                    else:
                        length_sql = stop - start + 1
                        expr = func.substring(str_val, start, length_sql)
                else:
                    # No stop means go to the end
                    expr = func.substring(str_val, start)
        else:
            raise ValueError(f"Unsupported string method: {method}")

        # Return as a subquery if the source was a subquery
        if isinstance(src, ColumnClause):
            return expr

        select_cols = [src.c.log_event_id.label("log_event_id")]
        if "__comp_idx__" in src.c.keys():
            select_cols.append(src.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in src.c.keys():
            select_cols.append(src.c.__parent_idx__.label("__parent_idx__"))
        select_cols.extend(
            [expr.label("value"), literal(inferred).label("inferred_type")],
        )
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(src),
            prefix="func_result",
        )
    else:
        # For literal values or direct SQL expressions
        if isinstance(src, BindParameter):
            if not isinstance(src.value, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"{method}() requires string input, got {type(src.value).__name__}",
                )

        str_val = cast(src, String)

        if method == "lower":
            return func.lower(str_val)
        elif method == "upper":
            return func.upper(str_val)
        elif method == "capitalize":
            # First char uppercase, rest lowercase:
            return func.concat(
                func.upper(func.substr(str_val, 1, 1)),
                func.lower(func.substr(str_val, 2)),
            )
        elif method == "strip":
            if args:
                chars = cast(args[0], String)
                return func.btrim(str_val, chars)
            else:
                return func.regexp_replace(str_val, "^\\s+|\\s+$", "", "g")
        elif method == "lstrip":
            if args:
                chars = cast(args[0], String)
                return func.ltrim(str_val, chars)
            else:
                return func.regexp_replace(str_val, "^\\s+", "", "g")
        elif method == "rstrip":
            if args:
                chars = cast(args[0], String)
                return func.rtrim(str_val, chars)
            else:
                return func.regexp_replace(str_val, "\\s+$", "", "g")
        elif method == "startswith":
            if not args:
                raise ValueError("startswith() requires a prefix argument")
            prefix = cast(args[0], String)
            return func.substr(str_val, 1, func.length(prefix)) == prefix
        elif method == "endswith":
            if not args:
                raise ValueError("endswith() requires a suffix argument")
            suffix = cast(args[0], String)
            return func.right(str_val, func.length(suffix)) == suffix
        elif method == "contains":
            if not args:
                raise ValueError("contains() requires a substring argument")
            substring = args[0]
            if isinstance(substring, BindParameter):
                substring_val = substring.value
                return func.position(str_val, substring_val) > 0
            else:
                return func.position(str_val, substring) > 0
        elif method == "match":
            if not args:
                raise ValueError("match() requires a pattern argument")
            pattern = args[0]
            return str_val.op("~")(pattern)
        elif method == "replace":
            if len(args) < 2:
                raise ValueError("replace() requires old and new substring arguments")
            old = args[0]
            new = args[1]
            return func.replace(str_val, old, new)
        elif method == "substring" or method == "__getitem__":
            # Handle both substring() and slice notation
            if method == "substring":
                if not args:
                    raise ValueError("substring() requires at least a start argument")
                start = args[0]
                length = args[1] if len(args) > 1 else None

                # PostgreSQL substring is 1-indexed
                if isinstance(start, BindParameter) and isinstance(start.value, int):
                    # Add 1 to convert from 0-indexed Python to 1-indexed PostgreSQL
                    start_val = start.value + 1 if start.value >= 0 else start.value
                    start = literal(start_val)
                else:
                    # For dynamic values, add 1 in the SQL
                    start = cast(start, Integer) + 1

                if length is not None:
                    length = cast(length, Integer)
                    return func.substring(str_val, start, length)
                else:
                    return func.substring(str_val, start)
            else:  # __getitem__ (slice notation)
                # Handle slice objects
                slice_obj = filter_dict.get("slice", {})
                start = slice_obj.get("start")
                stop = slice_obj.get("stop")

                if start is not None:
                    start = build_sql_query(
                        start,
                        log_event_alias,
                        session,
                        log_event_ids,
                        is_derived=is_derived,
                        local_scope=local_scope,
                        is_vector=is_vector,
                    )
                    # Convert to 1-indexed
                    if isinstance(start, BindParameter) and isinstance(
                        start.value,
                        int,
                    ):
                        start_val = start.value + 1 if start.value >= 0 else start.value
                        start = literal(start_val)
                    else:
                        start = cast(start, Integer) + 1
                else:
                    start = literal(1)  # Default to beginning of string

                if stop is not None:
                    stop = build_sql_query(
                        stop,
                        log_event_alias,
                        session,
                        log_event_ids,
                        is_derived=is_derived,
                        local_scope=local_scope,
                        is_vector=is_vector,
                    )
                    stop = cast(stop, Integer)
                    # Calculate length (stop - start)
                    if (
                        isinstance(stop, BindParameter)
                        and isinstance(stop.value, int)
                        and isinstance(start, BindParameter)
                        and isinstance(start.value, int)
                    ):
                        length = stop.value - start.value
                        return func.substring(str_val, start, literal(length))
                    else:
                        length_sql = stop - start + 1
                        return func.substring(str_val, start, length_sql)
                else:
                    # No stop means go to the end
                    return func.substring(str_val, start)
        else:
            raise ValueError(f"Unsupported string method: {method}")


def _handle_dict_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle dictionary comprehension expressions in filter queries.

    This function processes expressions like {k: v*2 for k, v in some_dict.items() if v > 0}
    by exploding the source dictionary into rows, then applying the transformations and
    filter to each element, and finally aggregating back into a dictionary.
    """
    # Check for pre-built iter subquery from JSONB wrapper
    # This supports wrapped JSONB expressions and existing subqueries
    if filter_dict.get("_jsonb_iter_subq") is not None:
        iter_subq = filter_dict["_jsonb_iter_subq"]
    else:
        iter_subq = build_sql_query(
            filter_dict["iter"],
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )

        # If iter_subq is a JSONB expression, wrap it as a subquery
        if _is_jsonb_expression(iter_subq):
            inferred_type = jsonb_builder._infer_expression_type(
                iter_subq,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            iter_subq = jsonb_builder._wrap_expression_as_subquery(
                iter_subq,
                inferred_type,
                log_event_alias,
                session,
                local_scope=local_scope,
                prefix="dict_comp_iter_wrapped",
            )

    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict comprehension source must be a subquery (JSONB expressions are auto-wrapped by the caller)",
        )

    if not local_scope:
        local_scope = {"__comp_base__": {}}

    val, val_type = _select_value(iter_subq, session, is_collection=True)
    # Fix: Include the subquery in FROM clause when checking type
    # This is necessary because val is a column reference (e.g., zipped.c.value)
    # and we need the subquery in the FROM clause to execute the type check
    is_array = (
        session.execute(
            select(func.jsonb_typeof(val)).select_from(iter_subq).limit(1),
        ).scalar()
        == "array"
    )
    if is_array:
        elem_tbl = (
            func.jsonb_array_elements(val)
            .table_valued("value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )
    else:
        elem_tbl = (
            func.jsonb_each(val)
            .table_valued("key", "value", with_ordinality="ordinality")
            .alias(name="elem_tbl")
        )

    parent_idx_col = _get_parent_idx(iter_subq.c)

    base_cols = [
        iter_subq.c.log_event_id,
        (elem_tbl.c.value.op("->>")(0) if is_array else elem_tbl.c.key).label(
            "__comp_key__",
        ),
        (elem_tbl.c.value.op("->")(1) if is_array else elem_tbl.c.value).label(
            "__comp_val__",
        ),
        elem_tbl.c.ordinality,
    ]
    if parent_idx_col is not None:
        base_cols.append(parent_idx_col.label("__parent_idx__"))

    base_stmt = select(*base_cols).select_from(
        iter_subq.outerjoin(elem_tbl, literal(True)),
    )
    base = alias_utils.subquery_with_unique_alias(base_stmt, prefix="base_dict_comp")

    # Fix: Include base in FROM clause when executing type inference queries
    comp_key_type = LogEventDAO.infer_type(
        "",
        session.execute(
            select(base.c.__comp_key__).select_from(base).limit(1),
        ).scalar(),
    )
    comp_val_type = LogEventDAO.infer_type(
        "",
        session.execute(
            select(base.c.__comp_val__).select_from(base).limit(1),
        ).scalar(),
    )

    local_scope = {
        filter_dict["target"][0]["value"]: (base.c.__comp_key__, comp_key_type),
        filter_dict["target"][1]["value"]: (base.c.__comp_val__, comp_val_type),
        "__comp_idx__": (base.c.ordinality, "int"),
        "__comp_base__": {
            **local_scope.pop("__comp_base__"),
            filter_dict["target"][0]["value"]: base,
            filter_dict["target"][1]["value"]: base,
        },
        **local_scope,
    }
    if parent_idx_col is not None:
        local_scope["__parent_idx__"] = (parent_idx_col, "int")

    def _value_column(expr):
        """
        If *expr* is a sub-query produced by build_sql_query return its
        `.c.value` column and make sure the caller knows it has to JOIN it.
        Otherwise just return *expr* unchanged.
        """
        if isinstance(expr, Subquery):
            has_idx = hasattr(expr.c, "__comp_idx__")
            return (
                expr.c.value,
                expr,
                has_idx,
            )
        return expr, None, False

    key_expr = build_sql_query(
        filter_dict["key_elt"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )

    val_expr = build_sql_query(
        filter_dict["val_elt"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )

    key_col, key_subq, key_has_idx = _value_column(key_expr)
    val_col, val_subq, val_has_idx = _value_column(val_expr)

    from_clause = base

    if key_subq is not None:
        key_with_row = select(
            key_subq.c.log_event_id,
            (
                key_subq.c.__comp_idx__ if key_has_idx else func.row_number().over()
            ).label("ordinality"),
            cast(key_subq.c.value, Text).label("value"),
            key_subq.c.inferred_type,
            *(
                [key_subq.c.__parent_idx__.label("__parent_idx__")]
                if hasattr(key_subq.c, "__parent_idx__")
                else []
            ),
        ).select_from(key_subq)
        key_with_row = alias_utils.subquery_with_unique_alias(
            key_with_row,
            prefix="key_with_row",
        )
        from_clause_with_key = select(
            from_clause.c.log_event_id,
            from_clause.c.ordinality,
            from_clause.c.__comp_key__,
            key_with_row.c.value.label("key_value"),
            key_with_row.c.inferred_type.label("key_type"),
            *(
                [key_with_row.c.__parent_idx__]
                if hasattr(from_clause.c, "__parent_idx__")
                and hasattr(key_with_row.c, "__parent_idx__")
                else []
            ),
        ).select_from(
            from_clause.outerjoin(
                key_with_row,
                and_(
                    from_clause.c.log_event_id == key_with_row.c.log_event_id,
                    from_clause.c.ordinality == key_with_row.c.ordinality,
                    *(
                        [
                            from_clause.c.__parent_idx__
                            == key_with_row.c.__parent_idx__,
                        ]
                        if hasattr(from_clause.c, "__parent_idx__")
                        and hasattr(key_with_row.c, "__parent_idx__")
                        else []
                    ),
                ),
            ),
        )
        from_clause_with_key = alias_utils.subquery_with_unique_alias(
            from_clause_with_key,
            prefix="from_clause_with_key",
        )
    else:
        from_clause_with_key = None

    if val_subq is not None:
        val_with_row = select(
            val_subq.c.log_event_id,
            (
                val_subq.c.__comp_idx__ if val_has_idx else func.row_number().over()
            ).label("ordinality"),
            val_subq.c.value,
            val_subq.c.inferred_type,
            *(
                [val_subq.c.__parent_idx__.label("__parent_idx__")]
                if hasattr(val_subq.c, "__parent_idx__")
                else []
            ),
        ).select_from(val_subq)
        val_with_row = alias_utils.subquery_with_unique_alias(
            val_with_row,
            prefix="val_with_row",
        )
        from_clause_with_val = select(
            from_clause.c.log_event_id,
            from_clause.c.ordinality,
            from_clause.c.__comp_val__,
            val_with_row.c.value.label("val_value"),
            val_with_row.c.inferred_type.label("val_type"),
            *(
                [val_with_row.c.__parent_idx__]
                if hasattr(from_clause.c, "__parent_idx__")
                and hasattr(val_with_row.c, "__parent_idx__")
                else []
            ),
        ).select_from(
            from_clause.outerjoin(
                val_with_row,
                and_(
                    from_clause.c.log_event_id == val_with_row.c.log_event_id,
                    from_clause.c.ordinality == val_with_row.c.ordinality,
                    *(
                        [
                            from_clause.c.__parent_idx__
                            == val_with_row.c.__parent_idx__,
                        ]
                        if hasattr(from_clause.c, "__parent_idx__")
                        and hasattr(val_with_row.c, "__parent_idx__")
                        else []
                    ),
                ),
            ),
        )
        from_clause_with_val = alias_utils.subquery_with_unique_alias(
            from_clause_with_val,
            prefix="from_clause_with_val",
        )
    else:
        from_clause_with_val = None

    final_key_col = None
    final_val_col = None

    if from_clause_with_key is not None and from_clause_with_val is not None:
        joined_clause = select(
            from_clause_with_key.c.log_event_id,
            from_clause_with_key.c.ordinality,
            from_clause_with_key.c.key_value,
            from_clause_with_val.c.val_value,
            *(
                [from_clause_with_key.c.__parent_idx__]
                if hasattr(from_clause_with_key.c, "__parent_idx__")
                else []
            ),
        ).select_from(
            from_clause_with_key.outerjoin(
                from_clause_with_val,
                and_(
                    from_clause_with_key.c.log_event_id
                    == from_clause_with_val.c.log_event_id,
                    from_clause_with_key.c.ordinality
                    == from_clause_with_val.c.ordinality,
                    *(
                        [
                            from_clause_with_key.c.__parent_idx__
                            == from_clause_with_val.c.__parent_idx__,
                        ]
                        if hasattr(from_clause_with_key.c, "__parent_idx__")
                        and hasattr(from_clause_with_val.c, "__parent_idx__")
                        else []
                    ),
                ),
            ),
        )
        joined_clause = alias_utils.subquery_with_unique_alias(
            joined_clause,
            prefix="joined_clause",
        )
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.val_value
    elif from_clause_with_key is not None:
        joined_clause = from_clause_with_key
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.__comp_val__
    elif from_clause_with_val is not None:
        joined_clause = from_clause_with_val
        final_key_col = joined_clause.c.__comp_key__
        final_val_col = joined_clause.c.val_value
    else:
        joined_clause = select(
            base.c.log_event_id,
            base.c.ordinality,
            base.c.__comp_key__.label("key_value"),
            base.c.__comp_val__.label("val_value"),
        ).select_from(base)
        joined_clause = alias_utils.subquery_with_unique_alias(
            joined_clause,
            prefix="joined_clause",
        )
        final_key_col = joined_clause.c.key_value
        final_val_col = joined_clause.c.val_value

    where_clause = literal(True)
    for cond_ast in filter_dict.get("ifs", []):
        cond_expr = build_sql_query(
            cond_ast,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )
        if isinstance(cond_expr, Subquery):
            condition = (
                select(cond_expr.c.value)
                .where(
                    cond_expr.c.log_event_id == joined_clause.c.log_event_id,
                    cond_expr.c.__comp_idx__ == joined_clause.c.ordinality,
                    *(
                        [cond_expr.c.__parent_idx__ == joined_clause.c.__parent_idx__]
                        if hasattr(cond_expr.c, "__parent_idx__")
                        and hasattr(joined_clause.c, "__parent_idx__")
                        else []
                    ),
                )
                .scalar_subquery()
            )
        else:
            condition = cond_expr
        where_clause = and_(where_clause, condition)

    if hasattr(joined_clause.c, "__parent_idx__"):
        final = (
            select(
                joined_clause.c.log_event_id,
                joined_clause.c.__parent_idx__.label("__comp_idx__"),
                func.coalesce(
                    func.jsonb_object_agg(final_key_col, final_val_col).filter(
                        final_key_col.isnot(None),
                    ),
                    literal({}, type_=JSONB),
                ).label("value"),
                literal("dict").label("inferred_type"),
            )
            .select_from(joined_clause)
            .where(where_clause)
            .group_by(joined_clause.c.log_event_id, joined_clause.c.__parent_idx__)
        )
        final = alias_utils.subquery_with_unique_alias(
            final,
            prefix="dict_comp_final",
        )
    else:
        final = (
            select(
                joined_clause.c.log_event_id,
                func.coalesce(
                    func.jsonb_object_agg(final_key_col, final_val_col).filter(
                        final_key_col.isnot(None),
                    ),
                    literal({}, type_=JSONB),
                ).label("value"),
                literal("dict").label("inferred_type"),
            )
            .select_from(joined_clause)
            .where(where_clause)
            .group_by(joined_clause.c.log_event_id)
        )
        final = alias_utils.subquery_with_unique_alias(
            final,
            prefix="dict_comp_final",
        )
    return final


def ensure_jsonb(expr):
    """
    Ensures an expression is cast to JSONB type.

    Args:
        expr: SQLAlchemy expression or literal value

    Returns:
        SQLAlchemy expression of JSONB type
    """
    # For Python literals / bind params, wrap with to_jsonb (handles any type)
    if isinstance(expr, BindParameter):
        return literal(expr.value, type_=JSONB)

    # If expression is already JSON/JSONB, leave as-is
    try:
        from sqlalchemy.dialects.postgresql import JSON as _PGJSON
        from sqlalchemy.dialects.postgresql import JSONB as _PGJSONB

        if isinstance(getattr(expr, "type", None), (_PGJSON, _PGJSONB)):
            return expr
    except Exception:
        # If inspection fails just fall through and convert
        pass

    # Fallback: use PostgreSQL's to_jsonb which can accept any SQL type
    return func.to_jsonb(expr)


def _handle_dict_get(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    default_supplied=False,
    is_vector=False,
    project_id=None,
    context_id=None,
):
    """
    Handle dictionary get() method in filter queries.

    This function processes expressions like my_dict.get('key', default_value)
    by extracting the value for the given key from a JSONB object and providing
    a default value if the key doesn't exist or the value is null.
    """
    # Check for zero arguments
    if "key" not in filter_dict:
        raise ValueError("dict.get() requires at least one argument (key)")

    # Check for too many arguments
    if len([k for k in filter_dict.keys() if k in ("key", "default")]) > 2:
        raise ValueError("dict.get() accepts at most 2 arguments (key, default)")

    # Build SQL for the dictionary container
    container_sql = build_sql_query(
        filter_dict["rhs"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    # Build SQL for the key
    key_sql = build_sql_query(
        filter_dict["key"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
    )

    # Build SQL for the default value if provided
    default_sql = None
    if "default" in filter_dict and filter_dict["default"] is not None:
        default_sql = build_sql_query(
            filter_dict["default"],
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )

    def process_get(container_val, key_val, default_val=None):
        """
        Process dictionary get operation with proper type handling.

        Args:
            container_val: JSONB container expression
            key_val: Key expression
            default_val: Optional default value expression

        Returns:
            tuple: (value_expr, result_type)
        """
        # Ensure container is JSONB
        container_jsonb = ensure_jsonb(container_val)

        # Ensure we're working with a JSON object, not an array or scalar
        is_object = func.jsonb_typeof(container_jsonb) == "object"

        # Use a CASE expression to handle non-object values safely
        safe_val = case((is_object, container_jsonb), else_=literal("{}", type_=JSONB))

        # Extract the raw JSONB value using -> operator (not ->> which returns text)
        key_text = cast(key_val, Text)
        extracted_jsonb = safe_val.op("->")(key_text)

        # Determine the type of the extracted value
        json_type = func.jsonb_typeof(extracted_jsonb)

        # Map JSON types to our type system with more granular handling
        result_type = case(
            (
                json_type == "number",
                # Check if it's an integer (no decimal part)
                case(
                    (
                        func.abs(cast(extracted_jsonb, Numeric) % 1) < 0.000001,
                        literal("int"),
                    ),
                    else_=literal("float"),
                ),
            ),
            (json_type == "boolean", literal("bool")),
            (json_type == "string", literal("str")),
            (json_type == "array", literal("list")),
            (json_type == "object", literal("dict")),
            (json_type == "null", literal("NoneType")),
            else_=literal("str"),
        )

        if default_val is not None:
            # Convert default to JSONB for proper coalescing
            default_jsonb = ensure_jsonb(default_val)

            # Get default type
            if isinstance(default_val, BindParameter):
                from orchestra.web.api.log.utils.type_utils import get_base_storage_type

                inferred = LogEventDAO.infer_type("", default_val.value)
                default_type = get_base_storage_type(inferred) or inferred
            elif isinstance(default_val, Subquery):
                _, default_type = _select_value(default_val, session)
            else:
                default_type = "str"

            # Coalesce at the JSONB level
            coalesced_jsonb = func.coalesce(extracted_jsonb, default_jsonb)

            # Determine the logical type of the value extracted from the dictionary, *before* coalescing.
            try:
                possible_types = [
                    row[0] for row in session.execute(select(result_type)).fetchall()
                ]
                from_type = (
                    unify_inferred_types(*possible_types) if possible_types else "str"
                )
            except Exception:
                from_type = "str"  # Fallback

            # Now, cast from the correctly inferred `from_type` to the `default_type`.
            value_expr = cast_expr(
                coalesced_jsonb,
                from_type,
                default_type,
            )
            return value_expr, default_type
        else:
            # No default - return JSONB value with "jsonb" type
            # This uses runtime type checking for truthiness evaluation,
            # ensuring correct behavior for all JSON types.
            return extracted_jsonb, "jsonb"

    # Handle subquery containers
    if isinstance(container_sql, Subquery):
        container_val, container_type = _select_value(
            container_sql,
            session,
            is_collection=True,
        )

        # Process key value
        if isinstance(key_sql, Subquery):
            key_val, _ = _select_value(key_sql, session)
        else:
            key_val = key_sql

        # Process default value if provided
        if default_sql is not None:
            if isinstance(default_sql, Subquery):
                default_val, _ = _select_value(default_sql, session)
            else:
                default_val = default_sql

            value_expr, result_type = process_get(container_val, key_val, default_val)
        else:
            value_expr, result_type = process_get(container_val, key_val)

        # Create the final subquery
        select_cols = [container_sql.c.log_event_id.label("log_event_id")]

        # Include composite indices if they exist
        if "__comp_idx__" in container_sql.c.keys():
            select_cols.append(container_sql.c.__comp_idx__.label("__comp_idx__"))
        if "__parent_idx__" in container_sql.c.keys():
            select_cols.append(container_sql.c.__parent_idx__.label("__parent_idx__"))

        # Add the value and type columns
        # Note: result_type may be a CASE expression (dynamic type) or a string literal
        if isinstance(result_type, str):
            type_col = literal(result_type).label("inferred_type")
        else:
            type_col = result_type.label("inferred_type")
        select_cols.extend([value_expr.label("value"), type_col])

        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(container_sql),
            prefix="func_result",
        )
    else:
        # For non-subquery containers (literals or direct SQL expressions)
        if default_sql is not None:
            value_expr, result_type = process_get(container_sql, key_sql, default_sql)
        else:
            value_expr, result_type = process_get(container_sql, key_sql)

        # For direct expressions, we need to wrap in a subquery if we have log_event_ids
        if log_event_ids is not None:
            if isinstance(log_event_ids, list):
                ids_subq = alias_utils.subquery_with_unique_alias(
                    select(literal(id_).label("log_event_id") for id_ in log_event_ids),
                    prefix="ids_list",
                )
            else:
                ids_subq = alias_utils.subquery_with_unique_alias(
                    select(log_event_ids.c.id.label("log_event_id")),
                    prefix="ids_subq",
                )

            return select(
                ids_subq.c.log_event_id,
                value_expr.label("value"),
                literal(result_type).label("inferred_type"),
            ).select_from(ids_subq)
            return alias_utils.subquery_with_unique_alias(
                subq,
                prefix="get_result",
            )
        else:
            # If no log_event_ids, just return the expression
            return value_expr


def _handle_zip(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
    is_vector=False,
):
    args = [
        build_sql_query(
            arg,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )
        for arg in filter_dict["rhs"]
    ]
    if not all(isinstance(arg, Subquery) for arg in args):
        raise HTTPException(
            status_code=400,
            detail="zip() expects only JSONB list columns",
        )

    zipped_subqs = []
    for idx, arg in enumerate(args):
        col, _ = _select_value(arg, session, is_collection=True)
        parent_idx_col = _get_parent_idx(arg.c)
        table_valued = (
            func.jsonb_array_elements(col)
            .table_valued("value", with_ordinality="ordinality")
            .alias(f"elem_tbl_{idx}")
        )
        sub_cols = [
            arg.c.log_event_id.label("log_event_id"),
            table_valued.c.ordinality.label("ordinality"),
            table_valued.c.value.label(f"value_{idx}"),
        ]
        if parent_idx_col is not None:
            sub_cols.append(parent_idx_col.label("__parent_idx__"))

        sub = alias_utils.subquery_with_unique_alias(
            select(*sub_cols).select_from(arg.join(table_valued, literal(True))),
            prefix=f"zip_subq_{idx}",
        )
        zipped_subqs.append(sub)

    base = zipped_subqs[0]
    for i, other in enumerate(zipped_subqs[1:], start=1):
        join_cond = and_(
            base.c.log_event_id == other.c.log_event_id,
            base.c.ordinality == other.c.ordinality,
            *(
                [base.c.__parent_idx__ == other.c.__parent_idx__]
                if "__parent_idx__" in base.c.keys()
                and "__parent_idx__" in other.c.keys()
                else []
            ),
        )
        base = select(
            base.c.log_event_id,
            base.c.ordinality,
            *[base.c[col] for col in base.c.keys() if col.startswith("value")],
            other.c[f"value_{i}"],
        ).select_from(
            base.join(
                other,
                join_cond,
            ),
        )
        base = alias_utils.subquery_with_unique_alias(
            base,
            prefix=f"zip_join_{i}",
        )

    value_columns = [base.c[col] for col in base.c.keys() if col.startswith("value")]

    select_cols = [
        base.c.log_event_id,
        func.coalesce(
            func.jsonb_agg(
                aggregate_order_by(
                    func.jsonb_build_array(*value_columns),
                    base.c.ordinality,
                ),
            ),
            literal([], type_=JSONB),
        ).label("value"),
        literal("list").label("inferred_type"),
    ]
    group_cols = [base.c.log_event_id]

    if "__parent_idx__" in base.c.keys():
        select_cols.insert(1, base.c.__parent_idx__)
        group_cols.append(base.c.__parent_idx__)

    zipped = alias_utils.subquery_with_unique_alias(
        select(*select_cols).group_by(*group_cols),
        prefix="zipped",
    )
    return zipped
