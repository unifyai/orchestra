from datetime import datetime, timezone

from fastapi import HTTPException
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    BindParameter,
    Boolean,
    Date,
    DateTime,
    Float,
    Numeric,
    String,
    Text,
    Time,
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

from orchestra.db.dao.log_dao import LogDAO, _is_date_string, _is_time_string
from orchestra.db.models.orchestra_models import Log

from .core import build_sql_query
from .helpers import (
    _build_subquery_for_base_call,
    _build_subquery_for_identifier,
    _ensure_vectors_exist,
    _get_embedding,
    _get_parent_idx,
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
                val_type == "timestamp",
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
        return select(*select_cols).select_from(rhs_expr).subquery()
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
):
    """
    Handles function-based operations ('len', 'str', 'type', 'round', 'round_timestamp',
    'exists', 'version', 'isNone', 'time', 'date', 'now', 'mean', 'sum', 'var', 'std',
    'min', 'max', 'median', 'mode', 'embed') in the filter dictionary.

    Args:
        filter_dict (dict): The filter dictionary containing the function and its arguments.
        log_event_alias: Alias for LogEvent to correlate subqueries.
        session: SQLAlchemy session for executing subqueries.

    Returns:
        SQLAlchemy condition or expression based on the provided function.
    """
    operand = filter_dict.get("operand")
    no_arg_functions = ["now"]
    two_arg_functions = ["BASE", "round", "round_timestamp", "embed"]

    if operand in no_arg_functions:
        rhs_expr = None
    elif operand in two_arg_functions:
        rhs_expr = [
            build_sql_query(
                expr,
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
                local_scope=local_scope,
            )
            for expr in filter_dict.get("rhs")
        ]
    else:
        # one_arg_functions
        rhs_expr = build_sql_query(
            filter_dict.get("rhs"),
            log_event_alias,
            session,
            log_event_ids=log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
        )

    if operand == "len":
        rval, rval_type = _select_value(rhs_expr, session)
        if isinstance(rhs_expr, (Subquery, ColumnClause)):
            expr = case(
                (
                    rval_type == "list",
                    func.jsonb_array_length(
                        cast(rval, JSONB),
                    ).cast(Float),
                ),
                (
                    rval_type == "dict",
                    select(func.count())
                    .select_from(
                        func.jsonb_object_keys(
                            cast(rval, JSONB),
                        ),
                    )
                    .scalar_subquery()
                    .cast(Float),
                ),
                (
                    rval_type == "str",
                    func.length(
                        func.replace(cast(rval, String), '"', ""),
                    ).cast(Float),
                ),
                else_=0,
            )
            if isinstance(rhs_expr, ColumnClause):
                return expr
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("int").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            return len(rhs_expr)

    elif operand == "str":
        if isinstance(rhs_expr, (Subquery, ColumnClause)):
            val, val_type = _select_value(rhs_expr, session)
            expr = func.cast(val, String)
            if isinstance(rhs_expr, ColumnClause):
                return expr
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("str").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            expr = rhs_expr[0] if isinstance(rhs_expr, list) else rhs_expr
            return cast(expr, String)

    elif operand == "round":
        # 1) Normalize the "rhs_expr" into a list of length 1 or 2
        if not isinstance(rhs_expr, list):
            rhs_expr = [rhs_expr]
        if len(rhs_expr) == 1:
            # round(val)
            val_expr = rhs_expr[0]
            if isinstance(val_expr, (Subquery, ColumnClause)):
                # subquery => we retrieve the numeric column
                val_col, val_type = _select_value(val_expr, session)
                # produce a new subquery
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in val_expr.c.keys():
                    select_cols.append(
                        val_expr.c.__parent_idx__.label("__parent_idx__"),
                    )
                expr = func.round(cast(val_col, Numeric))
                if isinstance(val_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            else:
                # val_expr is a literal or a direct SQL expression
                return func.round(cast(val_expr, Numeric))

        elif len(rhs_expr) == 2:
            # round(val, digits)
            val_expr, digits_expr = rhs_expr
            if isinstance(val_expr, Subquery) and isinstance(digits_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                dig_col = _select_value(digits_expr, session)
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in val_expr.c.keys():
                    select_cols.append(
                        val_expr.c.__parent_idx__.label("__parent_idx__"),
                    )
                expr = func.round(cast(val_col, Numeric), dig_col)
                if isinstance(val_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return (
                    select(*select_cols)
                    .select_from(val_expr)
                    .join(
                        digits_expr,
                        val_expr.c.log_event_id == digits_expr.c.log_event_id,
                    )
                    .subquery()
                )
            elif isinstance(val_expr, Subquery):
                val_col, val_type = _select_value(val_expr, session)
                select_cols = [val_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in val_expr.c.keys():
                    select_cols.append(val_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in val_expr.c.keys():
                    select_cols.append(
                        val_expr.c.__parent_idx__.label("__parent_idx__"),
                    )
                expr = func.round(cast(val_col, Numeric), digits_expr)
                if isinstance(val_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(val_expr).subquery()
            elif isinstance(digits_expr, Subquery):
                dig_col, dig_type = _select_value(digits_expr, session)
                # In that case, val_expr might be a literal
                select_cols = [digits_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in digits_expr.c.keys():
                    select_cols.append(digits_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in digits_expr.c.keys():
                    select_cols.append(
                        digits_expr.c.__parent_idx__.label("__parent_idx__"),
                    )
                expr = func.round(cast(val_col, Numeric), dig_col)
                if isinstance(digits_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("int").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(digits_expr).subquery()
            else:
                # both val_expr and digits_expr are non-subquery expressions (literals or direct SQL)
                return func.round(cast(val_expr, Numeric), digits_expr)
        else:
            raise ValueError("round(...) expects 1 or 2 arguments.")
    elif operand == "round_timestamp":
        if len(rhs_expr) != 2:
            raise ValueError(
                "round_timestamp(...) expects exactly 2 arguments: (timestamp_expr, seconds_expr)",
            )

        ts_expr = rhs_expr[0]
        sec_expr = rhs_expr[1]

        ts_is_sub = isinstance(ts_expr, Subquery)
        sec_is_sub = isinstance(sec_expr, Subquery)

        def _pg_round_timestamp(ts_col, seconds_col):
            ts_text = cast(ts_col, String)
            ts_cast = cast(ts_text, TIMESTAMP)
            return func.to_timestamp(
                func.round(
                    func.extract("epoch", ts_cast) / seconds_col,
                )
                * seconds_col,
            )

        if ts_is_sub and sec_is_sub:
            ts_col, ts_type = _select_value(ts_expr, session)
            sec_col, sec_type = _select_value(sec_expr, session)

            select_cols = [ts_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in ts_expr.c.keys():
                select_cols.append(ts_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in ts_expr.c.keys():
                select_cols.append(ts_expr.c.__parent_idx__.label("__parent_idx__"))
            expr = _pg_round_timestamp(ts_col, sec_col)
            if isinstance(ts_expr, ColumnClause):
                return expr
            select_cols.extend(
                [
                    expr.label("value"),
                    literal("timestamp").label("inferred_type"),
                ],
            )
            return (
                select(*select_cols)
                .select_from(ts_expr)
                .join(sec_expr, ts_expr.c.log_event_id == sec_expr.c.log_event_id)
                .subquery()
            )

        elif ts_is_sub:
            ts_col, ts_type = _select_value(ts_expr, session)
            if isinstance(sec_expr, BindParameter) and isinstance(
                sec_expr.value,
                (int, float),
            ):
                select_cols = [ts_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in ts_expr.c.keys():
                    select_cols.append(ts_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in ts_expr.c.keys():
                    select_cols.append(ts_expr.c.__parent_idx__.label("__parent_idx__"))
                expr = _pg_round_timestamp(ts_col, sec_expr.value)
                if isinstance(ts_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("timestamp").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(ts_expr).subquery()
            else:
                raise ValueError(
                    "round_timestamp() can't handle that form of seconds_expr (unless subquery).",
                )

        elif sec_is_sub:
            if isinstance(ts_expr, BindParameter) and isinstance(
                ts_expr.value,
                (datetime, str),
            ):
                ts_literal = literal(ts_expr.value, type_=TIMESTAMP)
                sec_col, sec_type = _select_value(sec_expr, session)

                select_cols = [sec_expr.c.log_event_id.label("log_event_id")]
                if "__comp_idx__" in sec_expr.c.keys():
                    select_cols.append(sec_expr.c.__comp_idx__.label("__comp_idx__"))
                if "__parent_idx__" in sec_expr.c.keys():
                    select_cols.append(
                        sec_expr.c.__parent_idx__.label("__parent_idx__"),
                    )
                expr = _pg_round_timestamp(ts_literal, sec_expr.value)
                if isinstance(sec_expr, ColumnClause):
                    return expr
                select_cols.extend(
                    [
                        expr.label("value"),
                        literal("timestamp").label("inferred_type"),
                    ],
                )
                return select(*select_cols).select_from(sec_expr).subquery()
            else:
                raise ValueError(
                    "round_timestamp() can't handle that form of timestamp_expr (unless subquery).",
                )

        else:
            if not isinstance(ts_expr, BindParameter) and not isinstance(
                ts_expr,
                (datetime, str),
            ):
                raise ValueError(
                    "Expected a literal datetime or string for the timestamp.",
                )
            if not isinstance(sec_expr, BindParameter) and not isinstance(
                sec_expr,
                (int, float),
            ):
                raise ValueError(
                    "Expected an integer or float literal for the rounding seconds.",
                )

            ts_lit = literal(ts_expr.value, type_=TIMESTAMP)
            return _pg_round_timestamp(ts_lit, sec_expr.value)

    elif operand == "exists":
        if (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("type") == "identifier"
        ):
            identifier = filter_dict["rhs"]["value"]
            subq = select(Log.id).filter(
                Log.log_event_id == log_event_alias.id,
                Log.key == identifier,
            )
            return subq.exists()
        else:
            raise ValueError(
                f"Invalid argument for 'exists' function: {filter_dict}",
            )

    elif operand == "version":
        if (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("type") == "identifier"
        ):
            identifier = filter_dict["rhs"]["value"]
            version_subq = (
                select(
                    Log.log_event_id.label("log_event_id"),
                    Log.version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .select_from(Log)
                .join(log_event_alias, Log.log_event_id == log_event_alias.id)
                .where(
                    Log.key == identifier,
                )
                .subquery()
            )
            return version_subq
        elif (
            isinstance(filter_dict.get("rhs"), dict)
            and filter_dict["rhs"].get("operand") == "BASE"
        ):
            base_args = filter_dict["rhs"].get("rhs", [])
            if len(base_args) != 2:
                raise ValueError(
                    "BASE(...) requires exactly 2 arguments: (event_id, key)",
                )

            event_ids = base_args[0]

            if base_args[1].get("type") == "identifier":
                identifier = base_args[1]["value"]
            else:
                raise ValueError(
                    f"Second argument to BASE must be an identifier, got: {base_args[1]}",
                )

            row_number = (
                func.row_number().over(order_by=Log.log_event_id).label("log_event_id")
            )
            version_subq = (
                select(
                    row_number.label("log_event_id"),
                    Log.version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .select_from(Log)
                .where(
                    Log.log_event_id.in_(event_ids) if event_ids else True,
                    Log.key == identifier,
                )
                .subquery()
            )
            return version_subq
        else:
            raise ValueError(f"Invalid argument for 'version' function: {filter_dict}")

    elif operand == "BASE":
        if len(rhs_expr) != 2:
            raise ValueError("BASE(...) requires exactly 2 arguments: (event_id, key)")

        event_id_expr = rhs_expr[0]
        key_expr = rhs_expr[1]
        return _build_subquery_for_base_call(
            event_id_expr,
            key_expr,
            session,
            log_event_ids,
            local_scope=local_scope,
        )
    elif operand == "isNone":
        if isinstance(filter_dict.get("rhs"), dict):
            rhs_expr = build_sql_query(
                filter_dict.get("rhs"),
                log_event_alias,
                session,
                log_event_ids=log_event_ids,
                is_derived=is_derived,
                local_scope=local_scope,
            )
        else:
            rhs_expr = [
                build_sql_query(
                    expr,
                    log_event_alias,
                    session,
                    log_event_ids=log_event_ids,
                    is_derived=is_derived,
                    local_scope=local_scope,
                )
                for expr in filter_dict.get("rhs")
            ]

        # If the rhs_expr is a Subquery, select its value and check is_(None)
        if isinstance(rhs_expr, (Subquery, ColumnClause)):
            rval, rval_type = _select_value(rhs_expr, session)
            if rval is None:
                return None
            expr = rval.is_(None)
            if isinstance(rhs_expr, ColumnClause):
                return expr
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__parent_idx__.label("__parent_idx__"))
            select_cols.extend(
                [expr.label("value"), literal("bool").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            # For non-subquery cases, simply return the boolean expression
            return rhs_expr.is_(None)

    elif operand == "time":
        if isinstance(rhs_expr, Subquery):
            val, val_type = _select_value(rhs_expr, session)

            # Create a CASE expression to handle different input types
            expr = case(
                (
                    val_type == "timestamp",
                    func.cast(
                        func.date_trunc(
                            "microseconds",
                            cast(cast(val, Text), DateTime(timezone=True)),
                        ),
                        Time,
                    ),
                ),
                (val_type == "str", func.cast(cast(val, Text), Time)),
                (val_type == "time", func.cast(cast(val, Text), Time)),
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
                [expr.label("value"), literal("time").label("inferred_type")],
            )
            return select(*select_cols).select_from(rhs_expr).subquery()
        else:
            if isinstance(rhs_expr, BindParameter):
                val = rhs_expr.value
                if isinstance(val, datetime):
                    return literal(val.time().isoformat(), type_=Time)
                elif isinstance(val, str) and _is_time_string(val):
                    clean_val = val.strip("\"'")
                    try:
                        if " PM" in clean_val or " AM" in clean_val:
                            for fmt in ("%I:%M %p", "%I:%M:%S %p", "%I:%M:%S.%f %p"):
                                try:
                                    dt = datetime.strptime(clean_val, fmt)
                                    return literal(dt.time().isoformat(), type_=Time)
                                except ValueError:
                                    continue
                        for fmt in ("%H:%M:%S", "%H:%M:%S.%f", "%H:%M"):
                            try:
                                dt = datetime.strptime(clean_val, fmt)
                                return literal(dt.time().isoformat(), type_=Time)
                            except ValueError:
                                continue
                        return literal(clean_val, type_=Time)
                    except Exception:
                        return literal(clean_val, type_=Time)
                else:
                    raise ValueError(
                        f"Cannot convert {val} to time. Expected datetime or time string.",
                    )
            else:
                return cast(rhs_expr, Time)

    elif operand == "date":
        return _handle_date_function(rhs_expr, session)
    elif operand == "now":
        if log_event_ids is None or log_event_ids == []:
            return literal(datetime.now(timezone.utc).isoformat(), type_=TIMESTAMP)

        if isinstance(log_event_ids, list):
            ids_subq = select(
                literal(id).label("log_event_id") for id in log_event_ids
            ).subquery()
            now_subq = (
                select(
                    ids_subq.c.log_event_id.label("log_event_id"),
                    func.timezone("UTC", func.now()).label("value"),
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ids_subq)
                .subquery()
            )
        else:
            ids_subq = log_event_ids
            row_number = (
                func.row_number().over(order_by=ids_subq.c.id).label("log_event_id")
            )
            event_id_col = row_number if is_derived else log_event_ids.c.id
            now_subq = (
                select(
                    event_id_col.label("log_event_id"),
                    func.timezone("UTC", func.now()).label("value"),
                    literal("timestamp").label("inferred_type"),
                )
                .select_from(ids_subq)
                .subquery()
            )
        return now_subq
    elif operand == "embed":
        # embed(text, model?, dimensions?) - Converts text to a vector embedding
        if len(rhs_expr) < 1 or len(rhs_expr) > 3:
            raise ValueError(
                "embed() requires 1-3 arguments: (text, [model], [dimensions])",
            )

        text_expr = rhs_expr[0]
        model_expr = rhs_expr[1] if len(rhs_expr) >= 2 else None
        dim_expr = rhs_expr[2] if len(rhs_expr) == 3 else None

        # Process model parameter if provided
        model = None
        if model_expr is not None:
            if isinstance(model_expr, BindParameter):
                model = model_expr.value
                if not isinstance(model, str):
                    raise ValueError(
                        f"embed() model must be a string, got {type(model).__name__}",
                    )
            else:
                raise ValueError("embed() requires a literal string as the model name")

        # Process dimensions parameter if provided
        dimensions = None
        if dim_expr is not None:
            if isinstance(dim_expr, BindParameter):
                dimensions = dim_expr.value
                if not isinstance(dimensions, int):
                    raise ValueError(
                        f"embed() dimensions must be an integer, got {type(dimensions).__name__}",
                    )
            else:
                raise ValueError(
                    "embed() requires a literal integer as the dimensions parameter",
                )

        # Handle text values (both column references and literals)
        if not isinstance(text_expr, BindParameter):
            # Get the key or identifier from the text expression
            key = None
            first_arg = filter_dict["rhs"][0]

            if first_arg.get("type") == "identifier":
                key = first_arg["value"]
            elif (
                first_arg.get("operand") == "BASE"
                and len(first_arg.get("rhs", [])) >= 2
                and first_arg["rhs"][1].get("type") == "identifier"
            ):
                key = first_arg["rhs"][1]["value"]
            else:
                raise ValueError("embed(): could not resolve key from first argument")

            # Ensure vectors exist for this key with the specified model
            # Fetch all text values for this key to create embeddings
            texts_q = select(Log.log_event_id, Log.value).where(Log.key == key)
            if isinstance(log_event_ids, list):
                texts_q = texts_q.where(Log.log_event_id.in_(log_event_ids))

            rows = session.execute(texts_q)
            id_to_text = {
                row.log_event_id: row.value
                for row in rows
                if isinstance(row.value, str)
            }

            if id_to_text:
                _ensure_vectors_exist(
                    session=session,
                    id_to_text=id_to_text,
                    model=model,
                    dimensions=dimensions,
                    key=key,
                )

            # Retrieve the vector column for the given key
            vector_subq = _build_subquery_for_identifier(
                key,
                log_event_alias,
                log_event_ids,
            )

            # Create a proper subquery with vector type
            select_cols = [vector_subq.c.log_event_id.label("log_event_id")]

            # Include composite indices if they exist
            if hasattr(vector_subq.c, "__comp_idx__"):
                select_cols.append(vector_subq.c.__comp_idx__.label("__comp_idx__"))
            if hasattr(vector_subq.c, "__parent_idx__"):
                select_cols.append(vector_subq.c.__parent_idx__.label("__parent_idx__"))

            # Add the vector value and type columns
            val_col, _ = _select_value(vector_subq, session)
            select_cols.extend(
                [
                    val_col.label("value"),
                    literal("vector").label("inferred_type"),
                ],
            )

            return select(*select_cols).select_from(vector_subq).subquery()
        else:
            # Handle literal text values (direct API call)
            text = text_expr.value
            if not isinstance(text, str):
                raise ValueError(
                    f"embed() requires a string, got {type(text).__name__}",
                )

            # Get the embedding vector
            embedding = _get_embedding(text, model, dimensions)

            # Create a vector literal using pgvector
            vector_expr = literal(embedding, type_=Vector(len(embedding)))

            return vector_expr

    elif operand in ["mean", "sum", "var", "std", "min", "max", "median", "mode"]:
        from ..utils.metric_utils import AggregationMetric, _get_reduction_expr

        reduction_functions = {
            "mean": AggregationMetric.MEAN,
            "sum": AggregationMetric.SUM,
            "var": AggregationMetric.VAR,
            "std": AggregationMetric.STD,
            "min": AggregationMetric.MIN,
            "max": AggregationMetric.MAX,
            "median": AggregationMetric.MEDIAN,
            "mode": AggregationMetric.MODE,
        }
        if isinstance(rhs_expr, (Subquery, ColumnClause)):
            val, val_type = _select_value(rhs_expr, session)
            agg_expr = _get_reduction_expr(
                reduction_functions[operand],
                val_type,
                val,
                "reduction_metric",
            )
            if isinstance(rhs_expr, ColumnClause):
                return agg_expr
            select_cols = [rhs_expr.c.log_event_id.label("log_event_id")]
            if "__comp_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__comp_idx__.label("__comp_idx__"))
            if "__parent_idx__" in rhs_expr.c.keys():
                select_cols.append(rhs_expr.c.__parent_idx__.label("__parent_idx__"))
            if val_type in ("list", "dict"):
                group_by_cols = [rhs_expr.c.log_event_id, rhs_expr.c.jsonb_value]
            else:
                group_by_cols = [rhs_expr.c.log_event_id]
            if "__comp_idx__" in rhs_expr.c.keys():
                group_by_cols.append(rhs_expr.c.__comp_idx__)
            if "__parent_idx__" in rhs_expr.c.keys():
                group_by_cols.append(rhs_expr.c.__parent_idx__)
            select_cols.extend(
                [agg_expr.label("value"), literal("float").label("inferred_type")],
            )
            return (
                select(*select_cols)
                .select_from(rhs_expr)
                .group_by(*group_by_cols)
                .subquery()
            )
        else:
            # For literal values or non-subquery cases
            if isinstance(rhs_expr, BindParameter):
                val = rhs_expr.value
                if isinstance(val, (list, tuple)):
                    # Convert Python list to JSONB array
                    jsonb_val = literal(val, type_=JSONB)
                    # Apply the reduction function directly
                    reduction_expr = _get_reduction_expr(
                        operand,
                        "list",
                        jsonb_val,
                        None,
                    )
                    return reduction_expr
                else:
                    raise ValueError(
                        f"Cannot apply {operand}() to non-list value: {val}",
                    )
            else:
                # For other SQL expressions, try to cast to JSONB and apply the function
                jsonb_expr = cast(rhs_expr, JSONB)
                reduction_expr = _get_reduction_expr(operand, "list", jsonb_expr, None)
                return reduction_expr
    else:
        raise ValueError(f"Unknown function operand: {operand}")


def _handle_dict_method(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    method = filter_dict["method"]  # e.g., "keys", "values", "items"
    src = build_sql_query(
        filter_dict["rhs"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived=is_derived,
        local_scope=local_scope,
    )
    if not isinstance(src, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict.keys/values/items only valid on JSONB column",
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

    base = select(*base_cols).select_from(src.join(each, true())).subquery("base")

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

    final = (
        select(*select_cols).group_by(*group_cols).subquery(f"dict_{method}_subquery")
    )
    return final


def _handle_if_expr(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
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
            return (
                select(*cols)
                .select_from(value)
                .subquery(
                    name=f"__inflate_select_subq_{value.name}",
                )
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

        return (
            select(*cols)
            .select_from(ids_subq)
            .subquery(name=f"__inflate_scalar_subq_{value}")
        )

    in_comprehension = local_scope is not None and ("__comp_idx__" in local_scope)
    raw_test = build_sql_query(
        filter_dict["test"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    raw_body = build_sql_query(
        filter_dict["body"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    raw_else = build_sql_query(
        filter_dict["orelse"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
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
            ids_subq = (
                union_all(*standardized_selects)
                .subquery(name="union_all_standardized_selects")
                .select()
                .distinct()
                .subquery(name="ids_subq")
            )
        else:
            ids_subq = (
                union_all(*id_selects)
                .subquery(name="union_all_id_selects")
                .select()
                .distinct()
                .subquery(name="ids_subq")
            )
    else:
        if isinstance(log_event_ids, Subquery):
            ids_subq = select(log_event_ids.c.id.label("log_event_id")).subquery()
        elif isinstance(log_event_ids, (list, tuple)):
            ids_subq = select(
                literal(id_).label("log_event_id") for id_ in log_event_ids
            ).subquery()
        else:
            ids_subq = select(log_event_alias.id.label("log_event_id")).subquery()

        if in_comprehension:
            comp_idx_col = local_scope["__comp_idx__"][0]
            ids_subq = (
                select(ids_subq.c.log_event_id, comp_idx_col.label("__comp_idx__"))
                .select_from(ids_subq)
                .subquery()
            )

    if not isinstance(raw_test, Subquery) or (
        isinstance(raw_test, Subquery) and "value" not in raw_test.columns
    ):
        raw_test = _inflate_scalar_or_subquery(
            raw_test,
            "bool"
            if not isinstance(raw_test, BindParameter)
            else LogDAO.infer_type("", raw_test.value),
            ids_subq,
            in_comprehension,
        )

    if not isinstance(raw_body, Subquery) or (
        isinstance(raw_body, Subquery) and "value" not in raw_body.columns
    ):
        raw_body = _inflate_scalar_or_subquery(
            raw_body,
            LogDAO.infer_type(
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
            LogDAO.infer_type(
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

    final_subq = (
        select(*select_cols)
        .select_from(
            ids_subq.join(raw_test, test_join_cond)
            .outerjoin(raw_body, body_join_cond)
            .outerjoin(raw_else, else_join_cond),
        )
        .subquery(name="final_subq")
    )

    return final_subq


def _handle_list_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    """
    Handle list comprehension expressions in filter queries.

    This function processes expressions like [x*2 for x in some_list if x > 0]
    by exploding the source list into rows, then applying the transformation and
    filter to each element, and finally aggregating back into a list.
    """
    iter_subq = build_sql_query(
        filter_dict["iter"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="list comprehension source must be a JSONB collection",
        )

    if not local_scope:
        local_scope = {"__comp_base__": {}}

    val, _ = _select_value(iter_subq, session, is_collection=True)
    is_array = session.execute(select(func.jsonb_typeof(val))).scalar() == "array"
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
    base = (
        select(*base_cols)
        .select_from(iter_subq.outerjoin(elem_tbl, literal(True)))
        .subquery("base_list_comp")
    )

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
            comp_type = LogDAO.infer_type(
                "",
                session.execute(select(comp_col)).scalar(),
            )
            local_scope[ident["value"]] = (comp_col, comp_type)
    else:
        comp_type = LogDAO.infer_type(
            "",
            session.execute(select(base.c.__comp_var__)).scalar(),
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
        elt_with_row = (
            select(
                elt_subq.c.log_event_id,
                (
                    elt_subq.c.__comp_idx__ if has_idx else func.row_number().over()
                ).label("ordinality"),
                *(
                    [elt_subq.c.__parent_idx__.label("__parent_idx__")]
                    if hasattr(elt_subq.c, "__parent_idx__")
                    else []
                ),
                elt_subq.c.value,
                elt_subq.c.inferred_type,
            )
            .select_from(elt_subq)
            .subquery(name="elt_with_row")
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
            .subquery(name="from_clause")
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
        .subquery(name="final")
    )
    return final


def _handle_dict_comp(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    """
    Handle dictionary comprehension expressions in filter queries.

    This function processes expressions like {k: v*2 for k, v in some_dict.items() if v > 0}
    by exploding the source dictionary into rows, then applying the transformations and
    filter to each element, and finally aggregating back into a dictionary.
    """
    iter_subq = build_sql_query(
        filter_dict["iter"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )
    if not isinstance(iter_subq, Subquery):
        raise HTTPException(
            status_code=400,
            detail="dict comprehension source must be JSONB list/dict",
        )

    if not local_scope:
        local_scope = {"__comp_base__": {}}

    val, _ = _select_value(iter_subq, session, is_collection=True)
    is_array = session.execute(select(func.jsonb_typeof(val))).scalar() == "array"
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

    base = (
        select(*base_cols)
        .select_from(iter_subq.outerjoin(elem_tbl, literal(True)))
        .subquery("base_dict_comp")
    )

    comp_key_type = LogDAO.infer_type(
        "",
        session.execute(select(base.c.__comp_key__)).scalar(),
    )
    comp_val_type = LogDAO.infer_type(
        "",
        session.execute(select(base.c.__comp_val__)).scalar(),
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
    )

    val_expr = build_sql_query(
        filter_dict["val_elt"],
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
    )

    key_col, key_subq, key_has_idx = _value_column(key_expr)
    val_col, val_subq, val_has_idx = _value_column(val_expr)

    from_clause = base

    if key_subq is not None:
        key_with_row = (
            select(
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
            )
            .select_from(key_subq)
            .subquery(name="key_with_row")
        )
        from_clause_with_key = (
            select(
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
            )
            .select_from(
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
            .subquery(name="from_clause_with_key")
        )
    else:
        from_clause_with_key = None

    if val_subq is not None:
        val_with_row = (
            select(
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
            )
            .select_from(val_subq)
            .subquery(name="val_with_row")
        )
        from_clause_with_val = (
            select(
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
            )
            .select_from(
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
            .subquery(name="from_clause_with_val")
        )
    else:
        from_clause_with_val = None

    final_key_col = None
    final_val_col = None

    if from_clause_with_key is not None and from_clause_with_val is not None:
        joined_clause = (
            select(
                from_clause_with_key.c.log_event_id,
                from_clause_with_key.c.ordinality,
                from_clause_with_key.c.key_value,
                from_clause_with_val.c.val_value,
                *(
                    [from_clause_with_key.c.__parent_idx__]
                    if hasattr(from_clause_with_key.c, "__parent_idx__")
                    else []
                ),
            )
            .select_from(
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
            .subquery(name="joined_clause")
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
        joined_clause = (
            select(
                base.c.log_event_id,
                base.c.ordinality,
                base.c.__comp_key__.label("key_value"),
                base.c.__comp_val__.label("val_value"),
            )
            .select_from(base)
            .subquery(name="joined_clause")
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
            .subquery(name="final")
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
            .subquery(name="final")
        )
    return final


def _handle_zip(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope=None,
):
    args = [
        build_sql_query(
            arg,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope=local_scope,
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

        sub = (
            select(*sub_cols)
            .select_from(arg.join(table_valued, literal(True)))
            .subquery(f"zip_subq_{idx}")
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
        base = (
            select(
                base.c.log_event_id,
                base.c.ordinality,
                *[base.c[col] for col in base.c.keys() if col.startswith("value")],
                other.c[f"value_{i}"],
            )
            .select_from(
                base.join(
                    other,
                    join_cond,
                ),
            )
            .subquery(name=f"zip_join_{i}")
        )

    value_columns = [base.c[col] for col in base.c.keys() if col.startswith("value")]

    select_cols = [
        base.c.log_event_id,
        func.coalesce(
            func.jsonb_agg(func.jsonb_build_array(*value_columns)),
            literal([], type_=JSONB),
        ).label("value"),
        literal("list").label("inferred_type"),
    ]
    group_cols = [base.c.log_event_id]

    if "__parent_idx__" in base.c.keys():
        select_cols.insert(1, base.c.__parent_idx__)
        group_cols.append(base.c.__parent_idx__)

    zipped = select(*select_cols).group_by(*group_cols).subquery("zipped")
    return zipped
