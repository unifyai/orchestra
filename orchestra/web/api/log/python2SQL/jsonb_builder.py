"""
Query builder for JSONB-based log storage. Translates Python filter expressions to PostgreSQL JSONB queries.
"""

import base64
import io
import logging
from typing import Any, Iterable, Optional, Tuple, Union

import imagehash
from pgvector.sqlalchemy import Vector
from PIL import Image
from sqlalchemy import (
    BindParameter,
    Boolean,
    Date,
    Float,
    Integer,
    String,
    Text,
    Time,
    and_,
    case,
    cast,
    func,
    literal,
    not_,
    or_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import BIT as Bit
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, Cast, ClauseElement
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.models.orchestra_models import Embedding, LogEvent
from orchestra.lib.parallel import threaded_map
from orchestra.services.bucket_service import BucketService

from . import alias_utils
from .ast_utils import get_identifier_value, is_identifier_node, parse_base_params
from .helpers import (
    _embeddable,
    _get_embedding,
    _get_field_type_from_db,
    _get_image_embedding_from_url,
    _infer_expression_type,
    _join_subqueries,
    _select_value,
    cast_expr,
    unify_inferred_types,
)
from .image_utils import get_phash_from_node
from .operators import _arithmetic_expr, _null_safe_eq, _null_safe_ne
from .subquery_utils import build_result_subquery, build_result_subquery_with_join
from .temporal_utils import (
    build_naive_datetime_subtraction_subquery,
    build_naive_timestamp_expr,
    strip_timezone_from_value,
)
from .type_mapping import normalize_field_type, normalize_type

__all__ = [
    "_build_jsonb_field_expression",
    "_handle_comparison_operator_jsonb",
    "_handle_arithmetic_operator_jsonb",
    "_handle_membership_operator_jsonb",
    "_handle_logical_operator_jsonb",
    "_handle_functions_jsonb",
    "_is_jsonb_expression",
    "_wrap_expression_as_subquery",
    "_handle_list_comp_jsonb",
    "_handle_dict_comp_jsonb",
    "_handle_if_expr_jsonb",
    "_handle_index_operator_jsonb",
    "_handle_slice_operator_jsonb",
    "_handle_dict_method_jsonb",
    "_handle_str_method_jsonb",
    "_infer_expression_type",
    "_handle_l2_jsonb",
    "_handle_cosine_jsonb",
    "_handle_ip_jsonb",
    "_handle_l1_jsonb",
    "_handle_euclidean_distance_jsonb",
    "_handle_jaccard_distance_jsonb",
    "_handle_phash_distance_jsonb",
    "_vector_binary_op_jsonb",
    "_ensure_numeric_iterable_jsonb",
    "_literal_vector_jsonb",
    "_coerce_to_vector_sql_jsonb",
]


# _normalize_type is now imported from type_mapping.py
# Alias for backward compatibility with local references
_normalize_type = normalize_type


def _is_list_type(type_str: str) -> bool:
    """Check if a type string represents a list/array type."""
    return _normalize_type(type_str) == "list"


def _is_dict_type(type_str: str) -> bool:
    """Check if a type string represents a dict/object type."""
    return _normalize_type(type_str) == "dict"


def _is_jsonb_like_type(type_str: str) -> bool:
    """Check if a type string represents a JSONB-like type (list, dict, jsonb)."""
    normalized = _normalize_type(type_str)
    return normalized in ("list", "dict", "jsonb")


def _is_simple_field_identifier(filter_dict: dict) -> tuple[bool, Optional[str]]:
    """
    Check if filter_dict represents a simple field identifier (e.g., 'status', 'score').

    Returns:
        (is_simple, field_name): True and field name if simple identifier, else (False, None)

    Simple identifiers:
        {"type": "identifier", "value": "field_name"} → (True, "field_name")

    NOT simple (returns False):
        {"operand": "INDEX", "lhs": {...}, "rhs": "a"}  # d.a property access
        {"operand": "+", "lhs": {...}, "rhs": {...}}    # arithmetic
        {"operand": "lower", "rhs": {...}}              # function call
    """
    if isinstance(filter_dict, dict) and filter_dict.get("type") == "identifier":
        return True, filter_dict.get("value")
    return False, None


def _is_scalar_literal(expr, expr_type: Optional[str] = None) -> bool:
    """
    Check if expression is a scalar literal value suitable for JSONB containment.

    Returns True for:
        - BindParameter with scalar values (int, float, str, bool, None)
        - Inferred types: int, float, str, bool, NoneType

    Returns False for:
        - Lists, dicts, tuples (complex JSON structures)
        - Subqueries (from embed, BASE, etc.)
        - Complex expressions (arithmetic, functions)
    """
    if not isinstance(expr, BindParameter):
        return False

    # Check if value is a scalar type
    value = expr.value
    if isinstance(value, (list, dict, tuple, set)):
        return False

    # If type is provided, verify it's a scalar type
    if expr_type:
        scalar_types = {"int", "float", "str", "bool", "NoneType"}
        return expr_type in scalar_types

    # Default: allow int, float, str, bool, None
    return isinstance(value, (int, float, str, bool, type(None)))


def _create_truthiness_condition_jsonb(expr, session, project_id=None, context_id=None):
    """
    Build SQL condition evaluating Python truthiness semantics for JSONB expressions.

    Handles subqueries, literals, and JSONB field references.
    """
    from sqlalchemy.sql.expression import Exists, UnaryExpression

    # Handle EXISTS and UnaryExpression directly
    if isinstance(expr, (Exists, UnaryExpression)):
        return expr

    # Handle BinaryExpression with boolean operators (comparisons)
    if isinstance(expr, BinaryExpression):
        # Check if operator is a comparison
        if hasattr(expr.operator, "__name__") and expr.operator.__name__ in (
            "eq",
            "ne",
            "lt",
            "le",
            "gt",
            "ge",
            "is_",
            "isnot",
            "like_op",
            "notlike_op",
            "ilike_op",
            "notilike_op",
            "contains_op",
            "not_contains_op",
            "startswith_op",
            "not_startswith_op",
            "endswith_op",
            "not_endswith_op",
        ):
            return expr
        # Check for JSONB existence operator (?) and containment (@>) which return boolean
        op_str = getattr(expr.operator, "opstring", str(expr.operator))
        if op_str in ("?", "@>", "<@", "LIKE", "ILIKE", "NOT LIKE", "NOT ILIKE"):
            return expr
        # Also check custom ops if needed, but usually comparisons are standard

    # Handle BindParameter (literal values)
    if isinstance(expr, BindParameter):
        return literal(bool(expr.value))

    # Handle Subquery - use _select_value to get the value column
    if isinstance(expr, Subquery):
        val_col, val_type = _select_value(
            expr,
            session,
            project_id=project_id,
            context_id=context_id,
        )
        if val_col is None:
            return literal(False)
        return _build_truthiness_sql(val_col, val_type)

    # Handle JSONB expressions (e.g., cast(data->>'field', Float))
    # Infer type and build appropriate truthiness condition
    from .helpers import _infer_expression_type

    inferred_type = _infer_expression_type(expr, session, project_id, context_id)
    return _build_truthiness_sql(expr, inferred_type)


# Import shared truthiness logic
from .truthiness import build_truthiness_sql as _build_truthiness_sql


def _build_jsonb_field_expression(
    key: str,
    log_event_alias,
    session,
    log_event_ids=None,
    is_derived=False,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
) -> Any:
    """
    Build a SQLAlchemy expression to extract a field from LogEvent.data JSONB column.

    In JSONB mode:
    - Regular fields are extracted from LogEvent.data using -> or ->> operators
    - Vector fields require joining with the Embedding table
    """
    # 1. Handle special fields (system columns from LogEvent table)
    # These are accessed via getattr to work with both aliased tables and the model class
    if key == "log_id":
        return getattr(log_event_alias, "id", None) or log_event_alias.c.id
    if key == "created_at":
        return (
            getattr(log_event_alias, "created_at", None) or log_event_alias.c.created_at
        )
    if key == "updated_at":
        return (
            getattr(log_event_alias, "updated_at", None) or log_event_alias.c.updated_at
        )

    # Validate that project_id is provided - required for field type lookup
    # If None, this indicates a bug in the caller not passing these parameters
    if project_id is None:
        raise ValueError(
            f"_build_jsonb_field_expression called with project_id=None for key='{key}'. "
            "This is a bug - project_id must be passed through the call chain for proper "
            "field type lookup in JSONB mode.",
        )

    if is_vector:
        # JSONB-native: Build a subquery joining LogEvent with Embedding table
        # This is used when directly accessing pre-computed embeddings
        #
        # Query the Embedding table to find which model was used for this key.
        # Embeddings can be created with different models (text: text-embedding-3-small,
        # image: multimodalembedding@001), so we need to dynamically detect the model.
        from .helpers import DEFAULT_EMBEDDING_MODEL

        # Look up the actual model used for this embedding key
        model_result = session.execute(
            select(Embedding.model).where(Embedding.key == key).limit(1),
        ).scalar()
        model_name = model_result if model_result else DEFAULT_EMBEDDING_MODEL

        # Use log_event_ids if available to scope the query to user's project/context
        if log_event_ids is not None:
            id_source = log_event_ids
            id_column = log_event_ids.c.id
        else:
            id_source = log_event_alias
            id_column = log_event_alias.id

        vector_subq = (
            select(
                id_column.label("log_event_id"),
                Embedding.vector.label("value"),
                literal("vector").label("inferred_type"),
            )
            .select_from(id_source)
            .outerjoin(
                Embedding,
                and_(
                    Embedding.ref_id == id_column,
                    Embedding.key == literal(key),
                    Embedding.model == literal(model_name),
                ),
            )
        )

        return alias_utils.subquery_with_unique_alias(
            vector_subq,
            prefix=f"vector_{key}",
        )

    if is_derived:
        # Derived values are stored directly in the data JSONB column
        pass

    # 2. Query FieldType
    raw_field_type = _get_field_type_from_db(key, session, project_id, context_id)

    # Normalize to SQL-compatible type (handles Pydantic schemas, Optional[T], etc.)
    from orchestra.web.api.log.utils.type_utils import get_sql_casting_type

    field_type = get_sql_casting_type(raw_field_type) if raw_field_type else None

    # 3. Build JSONB extraction
    jsonb_col = log_event_alias.data

    # Cast JSONB text extraction to target type based on FieldType
    if field_type == "float":
        raw_expr = jsonb_col.op("->>")(key)
        return cast_expr(raw_expr, "str", "float", force_to_type=True)
    elif field_type == "int":
        raw_expr = jsonb_col.op("->>")(key)
        return cast_expr(raw_expr, "str", "int", force_to_type=True)
    elif field_type == "bool":
        # Use direct cast for boolean - the -> operator returns JSONB which
        # PostgreSQL can directly cast to boolean. We don't use cast_expr here
        # because its bool handling has special truthiness logic not appropriate
        # for JSONB boolean extraction.
        return cast(jsonb_col.op("->")(key), Boolean)
    elif field_type == "str":
        # Use ->> operator for text extraction (type() handled separately via FieldType)
        return jsonb_col.op("->>")(key)
    elif field_type in ("datetime", "time", "date", "timedelta"):
        # Use cast_expr helper which expects raw expression and type
        raw_expr = jsonb_col.op("->>")(key)
        return cast_expr(raw_expr, "str", field_type)
    elif _is_list_type(field_type) or _is_dict_type(field_type):
        # Use -> to return JSONB object for list/dict types
        return jsonb_col.op("->")(key)

    # Default to text extraction
    return jsonb_col.op("->>")(key)


def _handle_comparison_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    query_context=None,
):
    """Handle comparison operators (==, !=, <, >, <=, >=, is, is not) in JSONB mode.

    Translates Python comparison expressions to PostgreSQL JSONB queries. Supports
    mixed compositions where one operand is a Subquery (from embed, phash, BASE)
    and the other is a JSONB expression.

    For simple field equality with scalar values, optimizes using GIN index via
    the @> containment operator when possible.

    Args:
        filter_dict: Parsed filter expression with 'operand', 'lhs', and 'rhs' keys.
        log_event_alias: SQLAlchemy alias for the LogEvent table.
        session: Database session for field type lookups.
        log_event_ids: Optional list of log event IDs to filter on.
        is_derived: Whether this is for a derived field expression.
        local_scope: Local variable bindings for comprehensions.
        is_vector: Whether the expression involves vector types.
        project_id: Project ID for field type lookups (required).
        context_id: Optional context ID for field type lookups.
        query_context: QueryContext for CTE-based aggregation optimization.

    Returns:
        SQLAlchemy expression representing the comparison, either as a boolean
        expression or a Subquery with value and inferred_type columns.
    """
    from .core import _build_sql_query_jsonb

    operand = filter_dict["operand"]
    lhs_dict = filter_dict["lhs"]
    rhs_dict = filter_dict["rhs"]

    lhs_expr = _build_sql_query_jsonb(
        lhs_dict,
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
    rhs_expr = _build_sql_query_jsonb(
        rhs_dict,
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

    lhs_is_sub = isinstance(lhs_expr, Subquery)
    rhs_is_sub = isinstance(rhs_expr, Subquery)

    # If both are JSONB expressions (common case), use simple direct comparison
    if not lhs_is_sub and not rhs_is_sub:
        # Handle list/dict literals in comparison - cast to JSONB if needed
        if isinstance(rhs_expr, BindParameter) and isinstance(
            rhs_expr.value,
            (list, dict),
        ):
            # If LHS is JSONB (which it usually is in this context), cast RHS to JSONB
            # This fixes jsonb = integer[] errors
            # Use func.to_jsonb for proper conversion of arrays/objects
            rhs_expr = func.to_jsonb(rhs_expr)

        if isinstance(lhs_expr, BindParameter) and isinstance(
            lhs_expr.value,
            (list, dict),
        ):
            lhs_expr = func.to_jsonb(lhs_expr)

        # Use JSONB containment (@>) for simple equality to leverage GIN index
        if operand == "==":
            # Check if LHS is a simple field identifier (not nested, not from function)
            lhs_is_simple, lhs_field_name = _is_simple_field_identifier(lhs_dict)

            if lhs_is_simple and lhs_field_name:
                # System columns (log_id, created_at, updated_at) are NOT in the JSONB
                # data column - they are direct columns on LogEvent table. Skip containment
                # optimization for these and fall through to regular comparison.
                system_columns = {"log_id", "created_at", "updated_at"}
                if lhs_field_name not in system_columns:
                    # Check if RHS is a scalar literal (not list/dict, not subquery)
                    rhs_type = _infer_expression_type(
                        rhs_expr,
                        session,
                        project_id,
                        context_id,
                    )

                    if _is_scalar_literal(rhs_expr, rhs_type):
                        # Skip containment optimization for None comparisons.
                        # Containment `data @> {"field": null}` only matches explicit null,
                        # not missing keys. Let None comparisons fall through to the
                        # special None handling below which checks for both cases.
                        if rhs_type == "NoneType":
                            pass  # Fall through to None handling
                        else:
                            # Verify field type is compatible with containment operator
                            # Containment works for: int, float, str, bool
                            # Does NOT work for: list, dict (these need special handling)
                            lhs_type = _infer_expression_type(
                                lhs_expr,
                                session,
                                project_id,
                                context_id,
                            )
                            containment_compatible_types = {
                                "int",
                                "float",
                                "str",
                                "bool",
                            }

                            if (
                                lhs_type in containment_compatible_types
                                or lhs_type is None
                                or lhs_type == "any"
                            ):
                                # Extract the literal value
                                rhs_value = rhs_expr.value

                                # Convert string "true"/"false" to Python bool for boolean field comparisons
                                if lhs_type == "bool" and rhs_type == "str":
                                    if isinstance(rhs_value, str):
                                        if rhs_value.lower() == "true":
                                            rhs_value = True
                                        elif rhs_value.lower() == "false":
                                            rhs_value = False
                                        # else: keep original string value, will likely not match

                                # Use JSONB containment for GIN index acceleration
                                # jsonb_build_object preserves Python types (int/float→number, str→string, bool→boolean)
                                containment_check = log_event_alias.data.op("@>")(
                                    func.jsonb_build_object(lhs_field_name, rhs_value),
                                )
                                return containment_check

        # Fall through to existing logic for complex cases
        # (nested access, arithmetic, functions, non-equality operators)

        lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)
        rhs_type = _infer_expression_type(rhs_expr, session, project_id, context_id)

        # Special handling for None comparisons in JSONB mode
        # JSONB null values can be either SQL NULL or the JSONB literal "null"
        # We need to check for both cases to properly match None comparisons
        # This applies to: `field == None`, `None == field`, `field != None`, `None != field`
        # and their `is`/`is not` variants
        if operand in ("is", "is not", "==", "!="):
            # Determine which side is None and which is the field expression
            if rhs_type == "NoneType":
                field_expr = lhs_expr
                is_equality = operand in ("is", "==")
            elif lhs_type == "NoneType":
                field_expr = rhs_expr
                is_equality = operand in ("is", "==")
            else:
                field_expr = None  # Neither side is None, skip this handling

            if field_expr is not None:
                # Cast field to text for comparison (handles both SQL NULL and JSONB "null")
                field_as_text = cast(field_expr, Text)
                if is_equality:
                    # `field is None` / `field == None` → field is SQL NULL OR field equals the string "null"
                    return or_(field_as_text.is_(None), field_as_text == "null")
                else:
                    # `field is not None` / `field != None` → field is NOT SQL NULL AND field is not "null"
                    return and_(field_as_text.isnot(None), field_as_text != "null")

        # Special handling for JSONB vs scalar comparison
        # When one side is "jsonb" (from nested access) and the other is a scalar type
        # (str, datetime, time, date, timedelta, int, float, bool), extracting JSONB
        # as text avoids "to_jsonb(unknown)" errors. The values stored in JSONB are
        # typically JSON strings that need to be extracted and compared as their target type.
        scalar_types = {
            "str",
            "datetime",
            "time",
            "date",
            "timedelta",
            "int",
            "float",
            "bool",
        }

        # Use centralized type helpers to check for JSONB-like expressions
        lhs_is_jsonb_like = _is_jsonb_like_type(lhs_type) if lhs_type else False
        rhs_is_jsonb_like = _is_jsonb_like_type(rhs_type) if rhs_type else False

        if lhs_is_jsonb_like and rhs_type in scalar_types:
            # Extract JSONB as text for comparison
            # Use hasattr to check if astext exists - BinaryExpressions from subscript don't have it
            if hasattr(lhs_expr, "astext"):
                lhs_as_text = lhs_expr.astext  # ->> operator
            else:
                # For BinaryExpression results (e.g., from JSONB array subscript), cast to text
                # and strip JSON quotes to get the raw value for comparison
                lhs_as_text = func.btrim(cast(lhs_expr, Text), literal('"'))
            # Use the RHS type for proper comparison (e.g., datetime >= datetime)
            # force_to_type=True bypasses type unification to ensure we cast to the target type
            lhs_casted = cast_expr(lhs_as_text, "str", rhs_type, force_to_type=True)
            rhs_casted = cast_expr(rhs_expr, rhs_type, rhs_type, force_to_type=True)
        elif lhs_type in scalar_types and rhs_is_jsonb_like:
            # Use hasattr to check if astext exists
            if hasattr(rhs_expr, "astext"):
                rhs_as_text = rhs_expr.astext
            else:
                # Strip JSON quotes from the text representation
                rhs_as_text = func.btrim(cast(rhs_expr, Text), literal('"'))
            lhs_casted = cast_expr(lhs_expr, lhs_type, lhs_type, force_to_type=True)
            rhs_casted = cast_expr(rhs_as_text, "str", lhs_type, force_to_type=True)
        else:
            unified_type = unify_inferred_types(lhs_type, rhs_type)
            lhs_casted = cast_expr(lhs_expr, lhs_type, unified_type)
            rhs_casted = cast_expr(rhs_expr, rhs_type, unified_type)

        if operand == "==":
            return lhs_casted == rhs_casted
        elif operand == "!=":
            return lhs_casted != rhs_casted
        elif operand == "<":
            return lhs_casted < rhs_casted
        elif operand == ">":
            return lhs_casted > rhs_casted
        elif operand == "<=":
            return lhs_casted <= rhs_casted
        elif operand == ">=":
            return lhs_casted >= rhs_casted
        elif operand == "is":
            return _null_safe_eq(lhs_casted, rhs_casted)
        elif operand == "is not":
            return _null_safe_ne(lhs_casted, rhs_casted)
        raise ValueError(f"Unknown comparison operand: {operand}")

    # Mixed case: at least one side is a Subquery (from embed, phash, BASE, etc.)
    # Join subquery with log_event_alias on log_event_id for mixed JSONB/subquery operands

    # Extract values and types
    lval, lval_type = _select_value(
        lhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )
    rval, rval_type = _select_value(
        rhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )

    unified_type = unify_inferred_types(lval_type, rval_type)
    lval_casted = cast_expr(lval, lval_type, unified_type)
    rval_casted = cast_expr(rval, rval_type, unified_type)

    # Build the comparison expression
    if operand == "==":
        expr = lval_casted == rval_casted
    elif operand == "!=":
        expr = lval_casted != rval_casted
    elif operand == "<":
        expr = lval_casted < rval_casted
    elif operand == ">":
        expr = lval_casted > rval_casted
    elif operand == "<=":
        expr = lval_casted <= rval_casted
    elif operand == ">=":
        expr = lval_casted >= rval_casted
    elif operand == "is":
        expr = _null_safe_eq(lval_casted, rval_casted)
    elif operand == "is not":
        expr = _null_safe_ne(lval_casted, rval_casted)
    else:
        raise ValueError(f"Unknown comparison operand: {operand}")

    # Wrap result in subquery to preserve log_event_id correlation
    if lhs_is_sub and rhs_is_sub:
        return _join_subqueries(lhs_expr, rhs_expr, expr, "bool", session=session)
    elif lhs_is_sub:
        # RHS is a JSONB expression - need to join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=lhs_expr,
            join_target=log_event_alias,
            join_condition=lhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type="bool",
            prefix="comparison_op",
        )
    else:  # rhs_is_sub
        # LHS is a JSONB expression - need to join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=rhs_expr,
            join_target=log_event_alias,
            join_condition=rhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type="bool",
            prefix="comparison_op",
        )


def _value_or_coalesce_jsonb(
    or_filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    query_context=None,
):
    """
    Build a CASE expression implementing Python's value-level `x or y`:
    returns x if truthy (non-empty for strings, non-null), else y.

    This handles expressions like `first_name or ''` where the result should be
    the actual value, not a boolean.
    """
    from .core import _build_sql_query_jsonb

    lhs_node = or_filter_dict.get("lhs")
    rhs_node = or_filter_dict.get("rhs")

    lhs_expr = _build_sql_query_jsonb(
        lhs_node,
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
    rhs_expr = _build_sql_query_jsonb(
        rhs_node,
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

    # Truthiness of LHS - use JSONB-aware truthiness check
    lhs_truthy = _create_truthiness_condition_jsonb(
        lhs_expr,
        session,
        project_id,
        context_id,
    )

    # Extract string/text values - cast to text for string coalescing
    lhs_text = cast(lhs_expr, String)
    rhs_text = cast(rhs_expr, String)

    # Return LHS if truthy, else RHS
    return case((lhs_truthy, lhs_text), else_=rhs_text)


def _handle_arithmetic_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    query_context=None,
):
    """Handle arithmetic operators (+, -, *, /, %, **, //) in JSONB mode.

    Translates Python arithmetic expressions to PostgreSQL queries. Supports:
    - Numeric operations with proper type casting
    - String concatenation via + (using || operator)
    - Temporal subtraction (datetime - datetime -> interval)
    - Value-level 'or' coalescing (e.g., `first_name or ''`)

    Args:
        filter_dict: Parsed filter expression with 'operand', 'lhs', and 'rhs' keys.
        log_event_alias: SQLAlchemy alias for the LogEvent table.
        session: Database session for field type lookups.
        log_event_ids: Optional list of log event IDs to filter on.
        is_derived: Whether this is for a derived field expression.
        local_scope: Local variable bindings for comprehensions.
        is_vector: Whether the expression involves vector types.
        project_id: Project ID for field type lookups (required).
        context_id: Optional context ID for field type lookups.
        query_context: QueryContext for CTE-based aggregation optimization.

    Returns:
        SQLAlchemy expression or Subquery representing the arithmetic result.
    """
    from .core import _build_sql_query_jsonb

    operand = filter_dict["operand"]
    lhs_dict = filter_dict["lhs"]
    rhs_dict = filter_dict["rhs"]

    # Rewrite value-level `or` used inside arithmetic into a coalescing expression
    # This handles expressions like `(first_name or '') + ' ' + (surname or '')`
    if isinstance(lhs_dict, dict) and lhs_dict.get("operand") == "or":
        lhs_expr = _value_or_coalesce_jsonb(
            lhs_dict,
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
    else:
        lhs_expr = _build_sql_query_jsonb(
            lhs_dict,
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

    if isinstance(rhs_dict, dict) and rhs_dict.get("operand") == "or":
        rhs_expr = _value_or_coalesce_jsonb(
            rhs_dict,
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
    else:
        rhs_expr = _build_sql_query_jsonb(
            rhs_dict,
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

    lhs_is_sub = isinstance(lhs_expr, Subquery)
    rhs_is_sub = isinstance(rhs_expr, Subquery)

    # If both are JSONB expressions (common case), use simple direct arithmetic
    if not lhs_is_sub and not rhs_is_sub:
        lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)
        rhs_type = _infer_expression_type(rhs_expr, session, project_id, context_id)

        # Check if date/time arithmetic
        if any(
            t in ("datetime", "date", "time", "timedelta") for t in (lhs_type, rhs_type)
        ):
            # Handle datetime subtraction with naive timestamps (timezone-agnostic)
            if operand == "-" and lhs_type == "datetime" and rhs_type == "datetime":
                # Use temporal_utils to build naive timestamp expressions
                lhs_ts = build_naive_timestamp_expr(lhs_dict, log_event_alias)
                rhs_ts = build_naive_timestamp_expr(rhs_dict, log_event_alias)

                if lhs_ts is not None and rhs_ts is not None:
                    return lhs_ts - rhs_ts

            expr, result_type = _arithmetic_expr(
                lhs_expr,
                rhs_expr,
                operand,
                lhs_type,
                rhs_type,
            )
            return expr

        # Check for string concatenation: if + operator and at least one side is string
        # PostgreSQL uses || for string concatenation, not +
        if operand == "+" and (lhs_type == "str" or rhs_type == "str"):
            lhs_str = cast(lhs_expr, String)
            rhs_str = cast(rhs_expr, String)
            return func.concat(lhs_str, rhs_str)

        # Numeric arithmetic
        unified_type = unify_inferred_types(lhs_type, rhs_type)
        if unified_type not in ("int", "float"):
            unified_type = "float"

        # Use force_to_type=True to ensure we cast to the numeric type
        # without cast_expr recalculating to a different type
        lhs_casted = cast_expr(lhs_expr, lhs_type, unified_type, force_to_type=True)
        rhs_casted = cast_expr(rhs_expr, rhs_type, unified_type, force_to_type=True)
        safe_rhs = func.nullif(rhs_casted, 0)

        if operand == "+":
            return lhs_casted + rhs_casted
        elif operand == "-":
            return lhs_casted - rhs_casted
        elif operand == "*":
            return lhs_casted * rhs_casted
        elif operand == "/":
            return lhs_casted / safe_rhs
        elif operand == "%":
            return lhs_casted % safe_rhs
        elif operand == "**":
            return func.power(lhs_casted, rhs_casted)
        elif operand == "//":
            return func.floor(lhs_casted / safe_rhs)
        raise ValueError(f"Unknown arithmetic operand: {operand}")

    # Mixed case: at least one side is a Subquery
    # When one side is a JSONB expression, we need to join with log_event_alias
    lval, lval_type = _select_value(
        lhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )
    rval, rval_type = _select_value(
        rhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )

    # Special handling for datetime subtraction: compare "wall clock" times, not UTC instants
    # This ensures "2023-06-15T12:00:00-05:00" - "2023-06-15T12:00:00+00:00" = PT0S
    # (both represent 12:00 local time) rather than PT5H (the UTC difference).
    if operand == "-" and lval_type == "datetime" and rval_type == "datetime":
        # The values have already been converted to TIMESTAMPTZ by safe_cast_to_timestamptz,
        # which converts to UTC. We need to access the RAW JSONB text to strip timezone.
        # Use ast_utils to parse BASE expressions
        lhs_base_ids, lhs_key = parse_base_params(lhs_dict)
        rhs_base_ids, rhs_key = parse_base_params(rhs_dict)

        # If we have both keys and base_ids, use temporal_utils for naive datetime subtraction
        if lhs_key and rhs_key and lhs_base_ids and rhs_base_ids:
            return build_naive_datetime_subtraction_subquery(
                lhs_key=lhs_key,
                rhs_key=rhs_key,
                lhs_base_ids=lhs_base_ids,
                rhs_base_ids=rhs_base_ids,
                lhs_expr=lhs_expr,
                lhs_is_sub=lhs_is_sub,
            )

        # Fallback: try stripping from converted values (may not work for UTC-converted values)
        lval_naive = strip_timezone_from_value(lval)
        rval_naive = strip_timezone_from_value(rval)
        expr = lval_naive - rval_naive
        result_type = "timedelta"
    else:
        # Compute the arithmetic expression
        expr, result_type = _arithmetic_expr(lval, rval, operand, lval_type, rval_type)

    # Wrap result in subquery to preserve log_event_id correlation
    if lhs_is_sub and rhs_is_sub:
        return _join_subqueries(lhs_expr, rhs_expr, expr, result_type, session=session)
    elif lhs_is_sub:
        # RHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=lhs_expr,
            join_target=log_event_alias,
            join_condition=lhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type=result_type,
            prefix="arithmetic_op",
        )
    else:  # rhs_is_sub
        # LHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=rhs_expr,
            join_target=log_event_alias,
            join_condition=rhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type=result_type,
            prefix="arithmetic_op",
        )


# Import shared pattern detection
from .truthiness import get_or_list_fallback as _get_or_list_fallback


def _handle_membership_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    query_context=None,
):
    """Handle membership operators ('in' and 'not in') in JSONB mode.

    Translates Python membership tests to PostgreSQL JSONB queries:
    - Array membership: Uses @> containment operator with GIN index
    - String membership: Uses LIKE or POSITION for substring checks
    - List literals: Converts to PostgreSQL array comparison
    - (expr or []) pattern: Safe iteration with COALESCE fallback

    Args:
        filter_dict: Parsed filter expression with 'operand', 'lhs', and 'rhs' keys.
        log_event_alias: SQLAlchemy alias for the LogEvent table.
        session: Database session for field type lookups.
        log_event_ids: Optional list of log event IDs to filter on.
        is_derived: Whether this is for a derived field expression.
        local_scope: Local variable bindings for comprehensions.
        is_vector: Whether the expression involves vector types.
        project_id: Project ID for field type lookups (required).
        context_id: Optional context ID for field type lookups.
        query_context: QueryContext for CTE-based aggregation optimization.

    Returns:
        Boolean expression or Subquery indicating membership result.
    """
    from .core import _build_sql_query_jsonb
    from .helpers import _substring_expr

    operand = filter_dict["operand"]
    lhs_dict = filter_dict["lhs"]
    rhs_dict = filter_dict["rhs"]

    # Handle the (expr or <list>) fallback pattern specially.
    # In Python, `'x' in (arr or [])` uses arr if truthy, else [].
    # We must NOT convert the `or` to boolean SQL OR - we need the array value.
    or_fallback_list = _get_or_list_fallback(rhs_dict)
    or_fallback_array_expr = None
    if or_fallback_list is not None:
        # Build only the LHS of the 'or' (the array expression)
        or_fallback_array_expr = _build_sql_query_jsonb(
            rhs_dict["lhs"],
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

    lhs_expr = _build_sql_query_jsonb(
        lhs_dict,
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

    # Use the or-fallback array expression if detected, otherwise build normally
    if or_fallback_array_expr is not None:
        rhs_expr = or_fallback_array_expr
    else:
        rhs_expr = _build_sql_query_jsonb(
            rhs_dict,
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

    lhs_is_sub = isinstance(lhs_expr, Subquery)
    rhs_is_sub = isinstance(rhs_expr, Subquery)
    is_in = operand == "in"

    # If both are JSONB expressions (common case)
    if not lhs_is_sub and not rhs_is_sub:
        lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)
        rhs_type = _infer_expression_type(rhs_expr, session, project_id, context_id)

        # Case 0: Handle (expr or <list>) fallback pattern for safe array membership
        # Use COALESCE to provide the fallback list when expression is NULL or JSON null
        if or_fallback_list is not None:
            import json

            fallback_json = json.dumps(or_fallback_list)
            # NULLIF converts JSON null to SQL NULL, then COALESCE provides fallback
            rhs_with_fallback = func.coalesce(
                func.nullif(cast(rhs_expr, JSONB), cast(literal("null"), JSONB)),
                cast(literal(fallback_json), JSONB),
            )
            containment = rhs_with_fallback.op("@>")(
                func.jsonb_build_array(lhs_expr),
            )
            return containment if is_in else not_(containment)

        # Case 1: RHS is JSONB array (use centralized type helper)
        if _is_list_type(rhs_type):
            containment = rhs_expr.op("@>")(
                func.jsonb_build_array(lhs_expr),
            )
            return containment if is_in else not_(containment)

        # Case 2: RHS is Python list literal or list of SQL expressions
        if isinstance(rhs_dict, list) or (
            isinstance(rhs_dict, dict) and rhs_dict.get("type") == "literal_list"
        ):
            rhs_list = None
            # Check if rhs_expr is a list of SQL expressions (from type_literal processing)
            if isinstance(rhs_expr, list):
                # rhs_expr is already a list of SQL expressions
                # Cast LHS to match the type of list elements
                lhs_casted = cast_expr(lhs_expr, lhs_type, lhs_type, force_to_type=True)
                return (
                    lhs_casted.in_(rhs_expr)
                    if is_in
                    else not_(lhs_casted.in_(rhs_expr))
                )
            elif hasattr(rhs_expr, "value"):
                rhs_list = rhs_expr.value
            if rhs_list is not None:
                # Cast LHS to match the type of list elements for proper comparison
                # This handles cases like: sender_id in [1, 2, 3, 4] where sender_id
                # is extracted as text but needs to be compared as integer
                lhs_casted = cast_expr(lhs_expr, lhs_type, lhs_type, force_to_type=True)
                return (
                    lhs_casted.in_(rhs_list)
                    if is_in
                    else not_(lhs_casted.in_(rhs_list))
                )

        # Case 3: Scalar field (substring) - only valid for strings
        if rhs_type == "str":
            if lhs_type != "str":
                lhs_expr = cast_expr(lhs_expr, lhs_type, "str")
            # Explicitly cast to String to ensure LIKE is used instead of @>
            rhs_str = cast(rhs_expr, String)
            contains_expr = rhs_str.contains(lhs_expr)
            return contains_expr if is_in else not_(contains_expr)

        # Case 4: NULL/NoneType - return False for membership in None (matches Python TypeError behavior)
        if _normalize_type(rhs_type) == "NoneType":
            return literal(False)

        # Case 5: Non-iterable types (bool, int, float, etc.)
        # In Python, `x in 5` or `x in True` raises TypeError
        # We return False (no matches) to align with Python semantics
        # where membership in non-iterables is invalid
        non_iterable_types = {
            "bool",
            "int",
            "float",
            "datetime",
            "date",
            "time",
            "timedelta",
        }
        if _normalize_type(rhs_type) in non_iterable_types:
            # Return a condition that's always False for `in`, always True for `not in`
            return literal(False) if is_in else literal(True)

        # Default fallback for dict types (substring on JSON representation)
        if _is_dict_type(rhs_type):
            lhs_str = cast(lhs_expr, String)
            rhs_str = cast(rhs_expr, String)
            contains_expr = rhs_str.like(func.concat("%", lhs_str, "%"))
            return contains_expr if is_in else not_(contains_expr)

        # Last resort fallback
        try:
            return lhs_expr.in_(rhs_expr) if is_in else not_(lhs_expr.in_(rhs_expr))
        except:
            lhs_str = cast(lhs_expr, String)
            rhs_str = cast(rhs_expr, String)
            contains_expr = rhs_str.like(func.concat("%", lhs_str, "%"))
            return contains_expr if is_in else not_(contains_expr)

    # Mixed case: at least one side is a Subquery
    # When one side is a JSONB expression, we need to join with log_event_alias
    lval, lval_type = _select_value(
        lhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )
    rval, rval_type = _select_value(
        rhs_expr,
        session,
        project_id=project_id,
        context_id=context_id,
    )

    # Build membership expression
    if _is_list_type(rval_type):
        expr = rval.op("@>")(func.jsonb_build_array(lval))
        expr = expr if is_in else not_(expr)
    else:
        # Substring check
        substring_cond = _substring_expr(lval, rval)
        expr = substring_cond if is_in else not_(substring_cond)

    # Wrap result in subquery to preserve log_event_id correlation
    if lhs_is_sub and rhs_is_sub:
        return _join_subqueries(lhs_expr, rhs_expr, expr, "bool", session=session)
    elif lhs_is_sub:
        # RHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=lhs_expr,
            join_target=log_event_alias,
            join_condition=lhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type="bool",
            prefix="membership_op",
        )
    else:  # rhs_is_sub
        # LHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=rhs_expr,
            join_target=log_event_alias,
            join_condition=rhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=expr,
            result_type="bool",
            prefix="membership_op",
        )


def _handle_logical_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id: Optional[int] = None,
    context_id: Optional[int] = None,
    query_context=None,
):
    """Handle logical operators (and, or, not) in JSONB mode.

    Implements Python truthiness semantics for JSONB expressions:
    - False values: None, False, 0, '', [], {}
    - True values: Everything else

    Uses CASE-based short-circuiting for performance optimization and
    handles mixed Subquery/JSONB compositions with proper outer joins.

    Args:
        filter_dict: Parsed filter expression with 'operand' and operands.
        log_event_alias: SQLAlchemy alias for the LogEvent table.
        session: Database session for field type lookups.
        log_event_ids: Optional list of log event IDs to filter on.
        is_derived: Whether this is for a derived field expression.
        local_scope: Local variable bindings for comprehensions.
        is_vector: Whether the expression involves vector types.
        project_id: Project ID for field type lookups (required).
        context_id: Optional context ID for field type lookups.
        query_context: QueryContext for CTE-based aggregation optimization.

    Returns:
        Boolean expression or Subquery representing the logical result.
    """
    from .core import _build_sql_query_jsonb

    operand = filter_dict["operand"]

    if operand == "not":
        rhs_dict = filter_dict["rhs"]
        rhs_expr = _build_sql_query_jsonb(
            rhs_dict,
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

        # Use JSONB-aware truthiness
        is_truthy = _create_truthiness_condition_jsonb(
            rhs_expr,
            session,
            project_id,
            context_id,
        )

        if isinstance(rhs_expr, Subquery):
            select_cols = [
                rhs_expr.c.log_event_id.label("log_event_id"),
                not_(is_truthy).label("value"),
                literal("bool").label("inferred_type"),
            ]
            return alias_utils.subquery_with_unique_alias(
                select(*select_cols).select_from(rhs_expr),
                prefix="logical_not",
            )
        return not_(is_truthy)

    lhs_dict = filter_dict["lhs"]
    rhs_dict = filter_dict["rhs"]

    lhs_expr = _build_sql_query_jsonb(
        lhs_dict,
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
    rhs_expr = _build_sql_query_jsonb(
        rhs_dict,
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

    lhs_is_sub = isinstance(lhs_expr, Subquery)
    rhs_is_sub = isinstance(rhs_expr, Subquery)

    # If both are JSONB expressions (common case), use simple direct logic
    if not lhs_is_sub and not rhs_is_sub:
        # Use JSONB-aware truthiness conditions for proper boolean semantics
        lhs_cond = _create_truthiness_condition_jsonb(
            lhs_expr,
            session,
            project_id,
            context_id,
        )
        rhs_cond = _create_truthiness_condition_jsonb(
            rhs_expr,
            session,
            project_id,
            context_id,
        )

        if operand == "and":
            return and_(lhs_cond, rhs_cond)
        elif operand == "or":
            return or_(lhs_cond, rhs_cond)
        raise ValueError(f"Unknown logical operand: {operand}")

    # Mixed case: at least one side is a Subquery
    # When one side is a JSONB expression, we need to join with log_event_alias
    # Use JSONB-aware truthiness conditions for proper short-circuit semantics
    lhs_cond = _create_truthiness_condition_jsonb(
        lhs_expr,
        session,
        project_id,
        context_id,
    )
    rhs_cond = _create_truthiness_condition_jsonb(
        rhs_expr,
        session,
        project_id,
        context_id,
    )

    if operand == "and":
        case_expr = case((lhs_cond, rhs_cond), else_=literal(False))
    elif operand == "or":
        case_expr = case((lhs_cond, literal(True)), else_=rhs_cond)
    else:
        raise ValueError(f"Unknown logical operand: {operand}")

    # Build subquery with proper join
    if lhs_is_sub and rhs_is_sub:
        is_full_join = operand == "or"
        from_clause = lhs_expr.outerjoin(
            rhs_expr,
            lhs_expr.c.log_event_id == rhs_expr.c.log_event_id,
            full=is_full_join,
        )
        select_cols = [
            func.coalesce(lhs_expr.c.log_event_id, rhs_expr.c.log_event_id).label(
                "log_event_id",
            ),
            case_expr.label("value"),
            literal("bool").label("inferred_type"),
        ]
        return alias_utils.subquery_with_unique_alias(
            select(*select_cols).select_from(from_clause),
            prefix="logical_op",
        )
    elif lhs_is_sub:
        # RHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=lhs_expr,
            join_target=log_event_alias,
            join_condition=lhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=case_expr,
            result_type="bool",
            prefix="logical_op",
        )
    else:  # rhs_is_sub
        # LHS is a JSONB expression - join subquery with log_event_alias
        return build_result_subquery_with_join(
            base_subq=rhs_expr,
            join_target=log_event_alias,
            join_condition=rhs_expr.c.log_event_id == log_event_alias.id,
            value_expr=case_expr,
            result_type="bool",
            prefix="logical_op",
        )


def _handle_functions_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle function calls in JSONB mode.

    Implements a wide range of functions for JSONB queries:
    - Type casting: int(), float(), bool(), str()
    - Existence: exists() using PostgreSQL ? operator
    - Length: len() for strings, arrays, and objects
    - Type checking: type() returns JSONB type name
    - Aggregations: mean(), sum(), var(), std(), min(), max(), median(), mode()
    - Date/time: date(), time(), now(), round_timestamp()
    - Math: round(), abs()
    - Text: num_tokens() for token counting

    When query_context is provided, aggregation functions use CTE-based
    optimization for pre-computation instead of correlated subqueries.

    Args:
        filter_dict: Parsed filter expression with 'operand' and arguments.
        log_event_alias: SQLAlchemy alias for the LogEvent table.
        session: Database session for field type lookups.
        log_event_ids: Optional list of log event IDs to filter on.
        is_derived: Whether this is for a derived field expression.
        local_scope: Local variable bindings for comprehensions.
        is_vector: Whether the expression involves vector types.
        project_id: Project ID for field type lookups.
        context_id: Optional context ID for field type lookups.
        query_context: Optional QueryContext for CTE-based aggregation optimization.
    """
    from orchestra.web.api.log.utils.metric_utils import (
        AggregationMetric,
        _get_reduction_expr,
    )

    from .core import _build_sql_query_jsonb

    operand = filter_dict.get("operand")

    if operand in ("int", "float", "bool"):
        # Handle explicit casting: int(x), float(x), bool(x)
        rhs = filter_dict.get("rhs")

        # rhs is the argument (unwrapped by parser if single arg, or list if multiple/list literal)
        # We pass it directly to _build_sql_query_jsonb
        arg_expr = _build_sql_query_jsonb(
            rhs,
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

        # Handle Subquery operands (from comprehensions, embed, BASE, etc.)
        if isinstance(arg_expr, Subquery):
            val_col, val_type = _select_value(
                arg_expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )

            if operand == "int":
                cast_result = cast(val_col, Integer)
            elif operand == "float":
                cast_result = cast(val_col, Float)
            else:  # bool
                cast_result = _create_truthiness_condition_jsonb(
                    val_col,
                    session,
                    project_id,
                    context_id,
                )

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=arg_expr,
                value_expr=cast_result,
                result_type=operand,
                prefix=f"{operand}_result",
            )

        # Infer type to help with casting
        inferred_type = _infer_expression_type(
            arg_expr,
            session,
            project_id,
            context_id,
        )

        # Cast to target type
        target_type = operand
        casted = cast_expr(arg_expr, inferred_type, target_type, force_to_type=True)
        return casted

    rhs_dict = filter_dict.get("rhs")

    # Helper to unpack single argument if passed as list (parser artifact?)
    def _get_single_arg(rhs):
        if isinstance(rhs, list) and len(rhs) == 1:
            return rhs[0]
        return rhs

    # --- Aggregation Functions ---
    if operand in [
        "mean",
        "sum",
        "var",
        "std",
        "min",
        "max",
        "median",
        "mode",
    ]:
        arg = _get_single_arg(rhs_dict)

        # Map operand to AggregationMetric enum
        metric_map = {
            "mean": AggregationMetric.MEAN,
            "sum": AggregationMetric.SUM,
            "var": AggregationMetric.VAR,
            "std": AggregationMetric.STD,
            "min": AggregationMetric.MIN,
            "max": AggregationMetric.MAX,
            "median": AggregationMetric.MEDIAN,
            "mode": AggregationMetric.MODE,
        }

        # For identifiers, look up field type directly from FieldType table
        # This is more reliable than inferring from the SQL expression type
        inferred_type = None
        # Check if identifier is in local_scope - if so, get type from there
        identifier_in_local_scope = False
        if is_identifier_node(arg):
            key = get_identifier_value(arg)
            if local_scope and key in local_scope:
                identifier_in_local_scope = True
                # Get type from local_scope tuple (col, type)
                _, scope_type = local_scope[key]
                if scope_type:
                    inferred_type = (
                        normalize_field_type(scope_type)
                        if isinstance(scope_type, str)
                        else scope_type
                    )
            else:
                ft = _get_field_type_from_db(key, session, project_id, context_id)
                if ft:
                    # Use type_mapping to normalize field type
                    inferred_type = normalize_field_type(ft)

        # Build argument expression
        expr = _build_sql_query_jsonb(
            arg,
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

        # Handle Subquery operands - extract the value column
        if isinstance(expr, Subquery):
            val_col, val_type = _select_value(
                expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )

            # Coerce ambiguous string types to float for numeric aggregations
            if val_type == "str" and operand in ("mean", "sum", "var", "std"):
                val_type = "float"

            agg_expr = _get_reduction_expr(
                metric_map[operand],
                val_type,
                val_col,
                "reduction_metric",
            )

            # For list/dict types, _get_reduction_expr returns a scalar subquery that references
            # the parent subquery's value column. We need to wrap this in a proper subquery
            # that includes the parent subquery in its FROM clause for proper correlation.
            if val_type in ("list", "dict", "Any"):
                # Build a proper wrapping subquery with GROUP BY
                from .subquery_utils import build_result_subquery_with_groupby

                return build_result_subquery_with_groupby(
                    base_subq=expr,
                    value_expr=agg_expr,
                    result_type="float",
                    prefix="aggregated",
                    include_value_in_groupby=True,
                )

            return agg_expr

        # If we didn't get type from FieldType lookup, infer from expression
        if inferred_type is None:
            inferred_type = _infer_expression_type(
                expr,
                session,
                project_id,
                context_id,
            )

        # Coerce string types to float for numeric aggregations
        if inferred_type == "str" and operand in ("mean", "sum", "var", "std"):
            inferred_type = "float"

        # For list/dict types, ensure we use JSONB extraction (-> not ->>)
        # so that _get_reduction_expr can properly use jsonb_array_elements
        # BUT: Skip this rebuild if identifier was found in local_scope - the expression
        # from local_scope is already correct (e.g., subq_a.c.data -> 'field')
        field_key = None
        if (
            _is_jsonb_like_type(inferred_type)
            and isinstance(arg, dict)
            and arg.get("type") == "identifier"
            and not identifier_in_local_scope  # Don't rebuild if from local_scope
        ):
            key = arg["value"]
            field_key = key
            # Rebuild expression using -> (JSONB) instead of ->> (TEXT)
            expr = log_event_alias.data.op("->")(key)

        # Register aggregation for CTE pre-computation when query_context provided
        if (
            query_context is not None
            and _is_jsonb_like_type(inferred_type)
            and isinstance(arg, dict)
            and arg.get("type") == "identifier"
        ):
            # Get field key for CTE naming
            if field_key is None:
                field_key = arg.get("value", "expr")

            # Build the JSONB field expression for CTE
            jsonb_field_expr = log_event_alias.data.op("->")(field_key)

            # Register aggregation for CTE pre-computation
            return query_context.register_aggregation(
                log_event_alias=log_event_alias,
                jsonb_field_expr=jsonb_field_expr,
                field_key=field_key,
                metric_name=operand,
                inferred_type=inferred_type,
            )

        # Fallback: Use existing correlated subquery approach
        return _get_reduction_expr(
            metric_map[operand],
            inferred_type,
            expr,
            "reduction_metric",
        )

    # --- Existence Check ---
    if operand == "exists":
        arg = _get_single_arg(rhs_dict)
        if isinstance(arg, dict) and arg.get("type") == "identifier":
            key = arg["value"]
            # PostgreSQL ? operator checks if key exists in JSONB object
            return log_event_alias.data.op("?")(literal(key))
        else:
            # Complex expression existence check
            expr = _build_sql_query_jsonb(
                arg,
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

            # Handle Subquery operands
            if isinstance(expr, Subquery):
                val_col, val_type = _select_value(
                    expr,
                    session,
                    project_id=project_id,
                    context_id=context_id,
                )

                if _is_jsonb_like_type(val_type) or val_type == "bool":
                    exists_expr = and_(
                        val_col.isnot(None),
                        func.jsonb_typeof(cast(val_col, JSONB)) != "null",
                    )
                else:
                    exists_expr = val_col.isnot(None)

                # Wrap in subquery to preserve log_event_id
                return build_result_subquery(
                    base_subq=expr,
                    value_expr=exists_expr,
                    result_type="bool",
                    prefix="exists_result",
                )

            # Check for both SQL NULL and JSON null for proper existence semantics
            inferred = _infer_expression_type(expr, session, project_id, context_id)

            if _is_jsonb_like_type(inferred) or inferred == "bool":
                # For JSONB types, ensure it's not 'null'::jsonb
                return and_(
                    expr.isnot(None),
                    func.jsonb_typeof(cast(expr, JSONB)) != "null",
                )
            else:
                # For scalar types (text, int, float), ->> returns SQL NULL if missing/null
                return expr.isnot(None)

    # --- Length Function ---
    if operand == "len":
        expr = _build_sql_query_jsonb(
            _get_single_arg(rhs_dict),
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

        # Handle Subquery operands (from embed, BASE, etc.)
        if isinstance(expr, Subquery):
            val_col, val_type = _select_value(
                expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )

            # Build length expression based on type
            if _is_list_type(val_type) or val_type == "vector":
                len_expr = cast(func.jsonb_array_length(cast(val_col, JSONB)), Integer)
            elif _is_dict_type(val_type):
                len_expr = cast(
                    select(func.count())
                    .select_from(
                        func.jsonb_object_keys(cast(val_col, JSONB)),
                    )
                    .scalar_subquery(),
                    Integer,
                )
            elif val_type == "str":
                len_expr = cast(func.length(cast(val_col, String)), Integer)
            else:
                len_expr = literal(0)

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=expr,
                value_expr=len_expr,
                result_type="int",
                prefix="len_result",
            )

        # Handle JSONB expressions directly
        inferred_type = _infer_expression_type(expr, session, project_id, context_id)

        # Use centralized type helpers for normalization
        if _is_list_type(inferred_type):
            return cast(func.jsonb_array_length(cast(expr, JSONB)), Integer)
        elif _is_dict_type(inferred_type):
            # Count keys in object using a scalar subquery
            return cast(
                select(func.count())
                .select_from(
                    func.jsonb_object_keys(cast(expr, JSONB)),
                )
                .scalar_subquery(),
                Integer,
            )
        elif inferred_type == "str":
            return cast(func.length(cast_expr(expr, inferred_type, "str")), Integer)
        else:
            return literal(0)

    # --- Type Inspection ---
    if operand == "type":
        arg = _get_single_arg(rhs_dict)
        # If identifier, try to look up in FieldType first
        if is_identifier_node(arg) and project_id is not None:
            key = get_identifier_value(arg)
            ft = _get_field_type_from_db(key, session, project_id, context_id)
            # Only use FieldType if it's a specific type, not "Any" (default/unknown)
            if ft and ft.lower() != "any":
                # Use type_mapping for normalization
                normalized = normalize_field_type(ft)
                return literal(normalized)

        # Fallback to runtime inspection
        expr = _build_sql_query_jsonb(
            arg,
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

        # Handle Subquery operands
        if isinstance(expr, Subquery):
            val_col, val_type = _select_value(
                expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )

            # If we have a known type, use it directly
            if val_type in ("int", "float", "str", "bool", "list", "dict", "vector"):
                type_expr = literal(val_type)
            else:
                # Use jsonb_typeof for runtime inspection
                pg_type = func.jsonb_typeof(cast(val_col, JSONB))
                type_expr = case(
                    (pg_type == "object", literal("dict")),
                    (pg_type == "array", literal("list")),
                    (pg_type == "string", literal("str")),
                    (pg_type == "number", literal("float")),
                    (pg_type == "boolean", literal("bool")),
                    (pg_type == "null", literal("NoneType")),
                    else_=literal("unknown"),
                )

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=expr,
                value_expr=type_expr,
                result_type="str",
                prefix="type_result",
            )

        # Use inferred type to distinguish int/float (jsonb_typeof returns 'number' for both)
        inferred = _infer_expression_type(expr, session, project_id, context_id)
        if inferred in ("int", "float"):
            return literal(inferred)

        # Map jsonb_typeof output to our type system
        # Use -> (JSONB) not ->> (text) for type checking to distinguish JSON null from SQL NULL

        # If arg is an identifier, use -> directly to preserve JSON null vs SQL NULL
        if isinstance(arg, dict) and arg.get("type") == "identifier":
            key = arg["value"]
            jsonb_expr = log_event_alias.data.op("->")(key)
        else:
            # For complex expressions, we need to cast to JSONB
            jsonb_expr = cast(expr, JSONB)

        pg_type = func.jsonb_typeof(jsonb_expr)
        return case(
            (pg_type == "object", literal("dict")),
            (pg_type == "array", literal("list")),
            (pg_type == "string", literal("str")),
            (
                pg_type == "number",
                literal("float"),
            ),  # Default to float for generic numbers
            (pg_type == "boolean", literal("bool")),
            (pg_type == "null", literal("NoneType")),
            else_=literal("unknown"),
        )

    # --- String Conversion ---
    if operand == "str":
        expr = _build_sql_query_jsonb(
            _get_single_arg(rhs_dict),
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

        # Handle Subquery operands (from embed, BASE, etc.)
        if isinstance(expr, Subquery):
            val_col, val_type = _select_value(
                expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            str_expr = cast(val_col, String)

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=expr,
                value_expr=str_expr,
                result_type="str",
                prefix="str_result",
            )

        return cast(expr, String)

    # --- Token Count ---
    if operand == "num_tokens":
        arg_dict = _get_single_arg(rhs_dict)

        # For identifiers, get raw text directly without type casting
        # This avoids issues with interval/timedelta conversions
        if isinstance(arg_dict, dict) and arg_dict.get("type") == "identifier":
            key = arg_dict["value"]
            # Get raw JSONB text value
            raw_expr = log_event_alias.data[key].astext  # Use ->> to get text
            # Handle NULL: return 0 for null field values, NULL for missing fields
            # Using CASE: if field exists, count bytes (coalesce 0 for null values)
            # If field doesn't exist, return NULL (which won't match == 0)
            byte_len = case(
                (
                    log_event_alias.data.has_key(key),
                    func.coalesce(func.octet_length(raw_expr), 0),
                ),
                else_=literal(None),
            )
            return cast(
                func.ceil(
                    cast(byte_len, Float) * literal(0.25),
                ),
                Float,
            )

        expr = _build_sql_query_jsonb(
            arg_dict,
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

        # Handle Subquery operands
        if isinstance(expr, Subquery):
            val_col, val_type = _select_value(
                expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            token_expr = cast(
                func.ceil(
                    cast(func.octet_length(cast(val_col, Text)), Float) * literal(0.25),
                ),
                Float,
            )

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=expr,
                value_expr=token_expr,
                result_type="float",
                prefix="num_tokens_result",
            )

        # Estimate: ceil(octet_length(text) * 0.25)
        # Explicitly cast result to Float to ensure numeric comparison
        return cast(
            func.ceil(
                cast(func.octet_length(cast(expr, Text)), Float) * literal(0.25),
            ),
            Float,
        )

    # --- Date/Time Functions ---
    if operand in ["date", "time", "now", "round_timestamp"]:
        if operand == "now":
            return func.timezone("UTC", func.now())

        return _handle_date_function_jsonb(
            operand,
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope,
            is_vector,
            project_id,
            context_id,
            query_context=query_context,
        )

    # --- Numeric Rounding ---
    if operand == "round":
        # Normalize rhs to list of arguments
        rhs = rhs_dict
        if not isinstance(rhs, list):
            rhs = [rhs]

        # Build each argument expression
        args = []
        for arg in rhs:
            args.append(
                _build_sql_query_jsonb(
                    arg,
                    log_event_alias,
                    session,
                    log_event_ids,
                    is_derived=is_derived,
                    local_scope=local_scope,
                    is_vector=is_vector,
                    project_id=project_id,
                    context_id=context_id,
                    query_context=query_context,
                ),
            )

        if not args:
            return literal(None)

        from sqlalchemy import Numeric

        # Check if first arg is a Subquery
        if isinstance(args[0], Subquery):
            val_col, val_type = _select_value(
                args[0],
                session,
                project_id=project_id,
                context_id=context_id,
            )
            val = cast(val_col, Numeric)

            if len(args) > 1:
                # Handle second argument (digits)
                if isinstance(args[1], Subquery):
                    digits_col, _ = _select_value(
                        args[1],
                        session,
                        project_id=project_id,
                        context_id=context_id,
                    )
                    digits = cast(digits_col, Integer)
                elif isinstance(args[1], BindParameter):
                    digits = cast(literal(args[1].value), Integer)
                else:
                    digits = cast(args[1], Integer)
                round_expr = func.round(val, digits)
            else:
                round_expr = func.round(val)

            # Wrap in subquery to preserve log_event_id
            return build_result_subquery(
                base_subq=args[0],
                value_expr=round_expr,
                result_type="float",
                prefix="round_result",
            )

        # Apply func.round with casting to Numeric
        val = cast(args[0], Numeric)
        if len(args) > 1:
            digits = cast(args[1], Integer)
            return func.round(val, digits)
        else:
            return func.round(val)

    # --- Special Values ---
    if operand == "isNone":
        # isNone(x) checks if x is None/NULL and returns a boolean
        # Example: "isNone(field1)" returns True if field1 is NULL
        # Returns True if the value is NULL/None
        arg_expr = _build_sql_query_jsonb(
            _get_single_arg(rhs_dict),
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

        # Handle Subquery operands
        if isinstance(arg_expr, Subquery):
            val_col, val_type = _select_value(
                arg_expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            is_none_expr = val_col.is_(None)
            return build_result_subquery(
                base_subq=arg_expr,
                value_expr=is_none_expr,
                result_type="bool",
                prefix="isnone_result",
            )

        # For direct expressions, return is None check
        return arg_expr.is_(None)

    if operand == "version":
        # Return param_version column if available, or NULL
        return literal(None)

    if operand == "BASE":
        # BASE(event_ids, key) is used for referencing fields from OTHER log events.
        # In JSONB mode, we query LogEvent.data directly for the specified event_ids.
        if not isinstance(rhs_dict, list) or len(rhs_dict) != 2:
            raise ValueError("BASE(...) requires exactly 2 arguments: (event_id, key)")

        # Build the event_ids expression
        event_id_expr = _build_sql_query_jsonb(
            rhs_dict[0],
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

        # Get the key name
        # The key can be either:
        # 1. A string (when field name contains special characters and was quoted)
        # 2. A dict with type="identifier" (standard field reference)
        key_dict = rhs_dict[1]
        if isinstance(key_dict, str):
            # Quoted field name (e.g., for "dt/est_time")
            key = key_dict
        elif isinstance(key_dict, dict) and key_dict.get("type") == "identifier":
            key = key_dict["value"]
        else:
            raise ValueError(
                f"BASE() second argument must be an identifier or string, got: {key_dict}",
            )

        # Extract base_ids from the expression
        if isinstance(event_id_expr, BindParameter):
            base_ids = event_id_expr.value
        elif isinstance(event_id_expr, list):
            base_ids = event_id_expr
        else:
            # Execute to get the value
            import json

            base_ids = session.execute(select(event_id_expr)).scalar()
            if isinstance(base_ids, str):
                base_ids = json.loads(base_ids)
            if not isinstance(base_ids, list):
                base_ids = [base_ids]

        # JSONB-native: Query LogEvent.data for the referenced log events
        from sqlalchemy.orm import aliased

        ref_log_event = aliased(LogEvent, name="base_ref_log_event")

        # Build JSONB field extraction for the key
        field_type = _get_field_type_from_db(key, session, project_id, context_id)

        # Normalize field type - handle "Any", None, and display types
        # Use get_base_storage_type to extract base type from nested types like "list[dict[str, int]]" -> "list"
        from orchestra.web.api.log.utils.type_utils import get_base_storage_type

        base_type = get_base_storage_type(field_type) if field_type else "any"
        normalized_type = base_type.lower() if base_type else "any"

        # Map display types and variations to internal types
        # This handles JSON Schema types, Python types, and common variations
        type_mapping = {
            # JSON Schema types
            "integer": "int",
            "number": "float",
            "string": "str",
            "boolean": "bool",
            "array": "list",
            "object": "dict",
            # Python type variations
            "nonetype": "str",  # Treat None as str for extraction
            "none": "str",
            # Temporal type variations
            "timestamp": "datetime",
            "interval": "timedelta",
        }
        normalized_type = type_mapping.get(normalized_type, normalized_type)

        # If type is "Any" or unknown, infer type from actual JSONB data
        # This handles all python2SQL types: int, float, str, bool, list, dict,
        # datetime, date, time, timedelta, vector, etc.
        if normalized_type in ("any", "none", ""):
            if base_ids:
                from orchestra.db.dao.log_dao import LogDAO
                from orchestra.web.api.log.utils.type_utils import get_base_storage_type

                # Sample ALL base_ids to find the first non-null type
                # This handles cases where some logs have null values for the field
                sample_query = (
                    select(func.jsonb_typeof(ref_log_event.data.op("->")(key)))
                    .where(ref_log_event.id.in_(base_ids))
                    .where(ref_log_event.data.op("->")(key).isnot(None))  # Skip nulls
                    .limit(1)
                )
                json_type = session.execute(sample_query).scalar()

                # If all values are null, try to get any non-null jsonb_typeof
                if json_type is None:
                    # All values are null or field doesn't exist - check if field exists at all
                    exists_query = (
                        select(func.jsonb_typeof(ref_log_event.data.op("->")(key)))
                        .where(ref_log_event.id.in_(base_ids))
                        .limit(1)
                    )
                    json_type = session.execute(exists_query).scalar()

                # Map PostgreSQL jsonb types to our internal types
                jsonb_type_mapping = {
                    "number": "float",  # Will refine below
                    "boolean": "bool",
                    "array": "list",
                    "object": "dict",
                    "null": "float",  # Default for null - assume numeric for arithmetic
                }

                if json_type == "string":
                    # For strings, sample the actual value and use LogDAO.infer_type
                    # to detect temporal types (datetime, date, time, timedelta)
                    value_sample = session.execute(
                        select(ref_log_event.data.op("->>")(key))
                        .where(ref_log_event.id.in_(base_ids))
                        .where(
                            ref_log_event.data.op("->>")(key).isnot(None),
                        )  # Skip nulls
                        .limit(1),
                    ).scalar()
                    if value_sample is not None:
                        # LogDAO.infer_type can detect datetime, date, time, timedelta from strings
                        inferred = LogDAO.infer_type(key, value_sample)
                        normalized_type = get_base_storage_type(inferred) or inferred
                    else:
                        normalized_type = "str"
                elif json_type == "number":
                    # For numbers, determine if int or float by checking string representation
                    # We use the string representation because PostgreSQL CAST requires it:
                    # - "25.0" can't be cast to INTEGER (has decimal point)
                    # - "25" can be cast to INTEGER
                    value_sample = session.execute(
                        select(ref_log_event.data.op("->>")(key))
                        .where(ref_log_event.id.in_(base_ids))
                        .where(
                            ref_log_event.data.op("->>")(key).isnot(None),
                        )  # Skip nulls
                        .limit(1),
                    ).scalar()
                    if value_sample is not None:
                        # Check if the string contains a decimal point or scientific notation
                        # If so, treat as float to avoid PostgreSQL cast errors
                        if "." in value_sample or "e" in value_sample.lower():
                            normalized_type = "float"
                        else:
                            try:
                                int(value_sample)  # Verify it's a valid integer literal
                                normalized_type = "int"
                            except (ValueError, TypeError):
                                normalized_type = "float"
                    else:
                        normalized_type = "float"
                elif json_type in jsonb_type_mapping:
                    normalized_type = jsonb_type_mapping[json_type]
                else:
                    normalized_type = (
                        "float"  # Default fallback - assume numeric for arithmetic
                    )
            else:
                normalized_type = (
                    "float"  # Default fallback - assume numeric for arithmetic
                )

        # Build the value expression based on type
        # Support all python2SQL types: numeric, boolean, temporal, collections
        raw_text_expr = ref_log_event.data.op("->>")(key)

        if normalized_type == "float":
            value_expr = cast(raw_text_expr, Float)
        elif normalized_type == "int":
            value_expr = cast(raw_text_expr, Integer)
        elif normalized_type == "bool":
            value_expr = cast(ref_log_event.data.op("->")(key), Boolean)
        elif normalized_type in ("list", "dict"):
            value_expr = ref_log_event.data.op("->")(key)
        elif normalized_type == "datetime":
            # Use safe cast function to handle invalid values gracefully (returns NULL instead of error)
            # This handles mixed types where some values are valid dates and some are garbage like "NULL"
            value_expr = cast_expr(raw_text_expr, "str", "datetime")
        elif normalized_type == "date":
            # Use safe cast function to handle invalid values gracefully
            value_expr = cast_expr(raw_text_expr, "str", "date")
        elif normalized_type == "time":
            # Use safe cast function to handle invalid values gracefully
            value_expr = cast_expr(raw_text_expr, "str", "time")
        elif normalized_type == "timedelta":
            # Use safe cast function to handle invalid values gracefully
            value_expr = cast_expr(raw_text_expr, "str", "timedelta")
        elif normalized_type == "vector":
            # Vector fields should be fetched from Embedding table, not JSONB
            # Build a subquery that joins with Embedding table
            from .helpers import DEFAULT_EMBEDDING_MODEL

            model_name = DEFAULT_EMBEDDING_MODEL

            # Build subquery selecting from Embedding table
            vector_subq = (
                select(
                    ref_log_event.id.label("log_event_id"),
                    Embedding.vector.label("value"),
                    literal("vector").label("inferred_type"),
                )
                .select_from(ref_log_event)
                .outerjoin(
                    Embedding,
                    and_(
                        Embedding.ref_id == ref_log_event.id,
                        Embedding.key == literal(key),
                        Embedding.model == literal(model_name),
                    ),
                )
                .where(ref_log_event.id.in_(base_ids))
            )

            return alias_utils.subquery_with_unique_alias(
                vector_subq,
                prefix=f"base_call_{key}",
            )
        else:
            # For "str" and unknown types, extract as text
            value_expr = raw_text_expr

        inferred_type = normalized_type

        # Build subquery selecting from referenced log events
        select_cols = [
            ref_log_event.id.label("log_event_id"),
            value_expr.label("value"),
            literal(inferred_type).label("inferred_type"),
        ]

        # Check if we're in a nested comprehension context
        # If so, we need to join with the outer comprehension's base to correlate log_event_id
        outer_comp_base = None
        if local_scope and "__comp_base__" in local_scope:
            comp_base = local_scope["__comp_base__"]
            if comp_base:
                # Get the first outer comprehension base (could be from dict-comp or list-comp)
                for base_key, base_subq in comp_base.items():
                    if isinstance(base_subq, Subquery):
                        outer_comp_base = base_subq
                        break

        # Add parent index if in comprehension context
        if local_scope and "__comp_idx__" in local_scope:
            parent_idx_col, _ = local_scope["__comp_idx__"]
            select_cols.insert(1, parent_idx_col.label("__comp_idx__"))

        # Build the query with proper correlation for nested comprehensions
        if outer_comp_base is not None:
            # Join with outer comprehension's base to correlate log_event_id
            # This ensures {log:field} accesses the same log event as the outer iteration
            base_subq = (
                select(*select_cols)
                .select_from(outer_comp_base)
                .join(
                    ref_log_event,
                    ref_log_event.id == outer_comp_base.c.log_event_id,
                )
                .where(ref_log_event.id.in_(base_ids))
            )
        else:
            base_subq = (
                select(*select_cols)
                .select_from(ref_log_event)
                .where(ref_log_event.id.in_(base_ids))
            )

        return alias_utils.subquery_with_unique_alias(
            base_subq,
            prefix=f"base_call_{key}",
        )

    # --- Embedding Functions ---
    if operand == "embed":
        return _handle_embed_jsonb(
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

    if operand == "embed_image":
        return _handle_embed_image_jsonb(
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

    if operand == "phash":
        return _handle_phash_jsonb(
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

    # zip function - handle locally for JSONB
    if operand == "zip":
        return _handle_zip_jsonb(
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

    if operand in ("jsonb_build_array", "jsonb_build_object"):
        rhs = rhs_dict
        if not isinstance(rhs, list):
            rhs = [rhs]

        args = []
        for arg in rhs:
            args.append(
                _build_sql_query_jsonb(
                    arg,
                    log_event_alias,
                    session,
                    log_event_ids,
                    is_derived=is_derived,
                    local_scope=local_scope,
                    is_vector=is_vector,
                    project_id=project_id,
                    context_id=context_id,
                    query_context=query_context,
                ),
            )

        if operand == "jsonb_build_array":
            return func.jsonb_build_array(*args)
        else:
            return func.jsonb_build_object(*args)

    raise NotImplementedError(f"JSONB support for function {operand} not implemented")


def _handle_date_function_jsonb(
    operand,
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived,
    local_scope,
    is_vector,
    project_id,
    context_id,
    query_context=None,
):
    """
    Handle date/time functions (date, time, round_timestamp) for JSONB.
    """
    from .core import _build_sql_query_jsonb

    rhs_dict = filter_dict.get("rhs")

    # Helper for timestamp rounding
    def _pg_round_timestamp(ts_col, seconds_col):
        # func.to_timestamp(func.round(func.extract('epoch', ts_col) / seconds_col) * seconds_col)
        # Note: ts_col might be JSONB, so we need to cast to timestamp first
        # Use safe_cast_to_timestamptz for proper type handling
        from sqlalchemy import Numeric as NumericType
        from sqlalchemy import Text

        # Cast to text first to get the string value, then to timestamp
        ts_as_text = func.replace(cast(ts_col, Text), '"', "")
        ts_as_timestamp = func.safe_cast_to_timestamptz(ts_as_text)

        epoch = func.extract("epoch", ts_as_timestamp)
        return func.to_timestamp(
            func.round(epoch / cast(seconds_col, NumericType)) * seconds_col,
        )

    if operand == "round_timestamp":
        # Expect exactly two arguments
        if not isinstance(rhs_dict, list) or len(rhs_dict) != 2:
            raise ValueError("round_timestamp requires exactly 2 arguments")

        ts_expr = _build_sql_query_jsonb(
            rhs_dict[0],
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
        sec_expr = _build_sql_query_jsonb(
            rhs_dict[1],
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

        # Handle Subquery for ts_expr
        if isinstance(ts_expr, Subquery):
            ts_val, _ = _select_value(
                ts_expr,
                session,
                project_id=project_id,
                context_id=context_id,
            )
            sec_val = (
                sec_expr.value if isinstance(sec_expr, BindParameter) else sec_expr
            )
            if isinstance(sec_expr, Subquery):
                sec_val, _ = _select_value(
                    sec_expr,
                    session,
                    project_id=project_id,
                    context_id=context_id,
                )

            round_expr = _pg_round_timestamp(ts_val, sec_val)

            # Wrap in subquery
            return build_result_subquery(
                base_subq=ts_expr,
                value_expr=round_expr,
                result_type="datetime",
                prefix="round_timestamp_result",
            )

        return _pg_round_timestamp(ts_expr, sec_expr)

    # For single-argument date/time functions
    expr = _build_sql_query_jsonb(
        rhs_dict,
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

    # Handle Subquery operands
    if isinstance(expr, Subquery):
        val_col, val_type = _select_value(
            expr,
            session,
            project_id=project_id,
            context_id=context_id,
        )

        if operand == "date":
            if val_type == "datetime":
                result_expr = cast(func.date_trunc("day", val_col), Date)
            else:
                result_expr = cast_expr(val_col, val_type, "date")
            result_type = "date"
        elif operand == "time":
            if val_type == "datetime":
                result_expr = cast(val_col, Time)
            else:
                result_expr = cast_expr(val_col, val_type, "time")
            result_type = "time"
        else:
            raise NotImplementedError(
                f"Date function {operand} not fully implemented for JSONB",
            )

        # Wrap in subquery
        return build_result_subquery(
            base_subq=expr,
            value_expr=result_expr,
            result_type=result_type,
            prefix=f"{operand}_result",
        )

    inferred_type = _infer_expression_type(expr, session, project_id, context_id)

    if operand == "date":
        # Special handling for datetime -> date conversion
        if inferred_type == "datetime":
            return cast(func.date_trunc("day", expr), Date)
        return cast_expr(expr, inferred_type, "date")

    if operand == "time":
        # Special handling for datetime -> time
        if inferred_type == "datetime":
            return cast(expr, Time)
        return cast_expr(expr, inferred_type, "time")

    raise NotImplementedError(
        f"Date function {operand} not fully implemented for JSONB",
    )


def _wrap_expression_as_subquery(
    expr,
    inferred_type: str,
    log_event_alias,
    session,
    local_scope=None,
    prefix: str = "wrapped_expr",
):
    """
    Wrap a JSONB expression in a subquery with standard columns (log_event_id, value, inferred_type).

    Enables comprehension and method handlers to work with JSONB expressions by converting them
    to the expected subquery format. Propagates index columns for nested comprehensions.
    """
    from sqlalchemy.sql.selectable import Subquery

    from . import alias_utils

    # Determine the base to select from
    # For nested comprehensions, we may need to use a base subquery from local_scope
    from_clause = log_event_alias
    log_event_id_col = log_event_alias.id

    # Check if we're in a nested comprehension context with a base subquery
    if local_scope:
        comp_base = local_scope.get("__comp_base__", {})
        if comp_base:
            # Use the first base subquery if available
            for key, base_subq in comp_base.items():
                if isinstance(base_subq, Subquery):
                    from_clause = base_subq
                    log_event_id_col = base_subq.c.log_event_id
                    break

    # Build select columns
    select_cols = [
        log_event_id_col.label("log_event_id"),
    ]

    # Include __comp_idx__ if present in local_scope (for comprehensions)
    if local_scope and "__comp_idx__" in local_scope:
        idx_col, _ = local_scope["__comp_idx__"]
        select_cols.append(idx_col.label("__comp_idx__"))

    # Include __parent_idx__ if present (for nested comprehensions)
    if local_scope and "__parent_idx__" in local_scope:
        parent_col, _ = local_scope["__parent_idx__"]
        select_cols.append(parent_col.label("__parent_idx__"))

    # Add ordinality column if we're wrapping for iteration (list type)
    # This helps maintain proper element ordering in comprehensions
    if _is_list_type(inferred_type) and from_clause is log_event_alias:
        # For top-level list expressions, add a placeholder ordinality
        # The actual ordinality will be provided by jsonb_array_elements
        pass  # Ordinality is handled by the comprehension handler

    # Add value and type columns
    # Cast to JSONB for collection types to preserve structure
    # Use centralized type helpers for consistent normalization
    if _is_list_type(inferred_type) or _is_dict_type(inferred_type):
        value_col = cast(expr, JSONB).label("value")
    else:
        value_col = expr.label("value")

    select_cols.extend(
        [
            value_col,
            literal(inferred_type).label("inferred_type"),
        ],
    )

    subq = select(*select_cols).select_from(from_clause)

    return alias_utils.subquery_with_unique_alias(subq, prefix=prefix)


def _is_jsonb_expression(expr) -> bool:
    """
    Detect if an object is a JSONB expression or a Subquery.

    Returns True if it's a JSONB expression (BinaryExpression with JSONB operators,
    Cast expressions, or direct column references), False if it's a Subquery.
    """
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import Subquery

    if isinstance(expr, Subquery):
        return False

    # If it's a Cast, BinaryExpression, or other ColumnElement, it's an expression
    if isinstance(expr, (BinaryExpression, Cast)):
        return True

    # Check for ColumnElement (covers most SQLAlchemy expressions)
    if isinstance(expr, ColumnElement):
        return True

    return False


def _handle_list_comp_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle list comprehension expressions like [x*2 for x in scores if x > 0].

    Builds the iterable expression, wraps it as a subquery if needed, and processes
    the comprehension logic.
    """
    from .core import _build_sql_query_jsonb
    from .functions import _handle_list_comp

    # Build the iterable expression
    iter_expr = _build_sql_query_jsonb(
        filter_dict["iter"],
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

    # If result is a direct JSONB expression (not a subquery), wrap it
    if _is_jsonb_expression(iter_expr):
        inferred_type = _infer_expression_type(
            iter_expr,
            session,
            project_id,
            context_id,
        )
        iter_expr = _wrap_expression_as_subquery(
            iter_expr,
            inferred_type,
            log_event_alias,
            session,
            local_scope=local_scope,
            prefix="list_comp_iter_jsonb",
        )

    # Create a modified filter_dict with the wrapped iter subquery
    # The shared handler will use _jsonb_iter_subq instead of rebuilding from filter_dict["iter"]
    modified_filter = {**filter_dict, "_jsonb_iter_subq": iter_expr}

    # Delegate to the shared handler which will use the wrapped subquery
    return _handle_list_comp(
        modified_filter,
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )


def _handle_dict_comp_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle dictionary comprehension expressions in JSONB mode.

    Similar to list comprehensions but for {k: v*2 for k, v in items.items() if v > 0}.
    Wraps JSONB expressions as subqueries and delegates to shared handler logic.
    """
    from .core import _build_sql_query_jsonb
    from .functions import _handle_dict_comp

    # Build the iterable expression
    iter_expr = _build_sql_query_jsonb(
        filter_dict["iter"],
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

    # If result is a direct JSONB expression, wrap it
    if _is_jsonb_expression(iter_expr):
        inferred_type = _infer_expression_type(
            iter_expr,
            session,
            project_id,
            context_id,
        )
        iter_expr = _wrap_expression_as_subquery(
            iter_expr,
            inferred_type,
            log_event_alias,
            session,
            local_scope=local_scope,
            prefix="dict_comp_iter_jsonb",
        )

    # Create a modified filter_dict with the wrapped iter subquery
    # The shared handler will use _jsonb_iter_subq instead of rebuilding from filter_dict["iter"]
    modified_filter = {**filter_dict, "_jsonb_iter_subq": iter_expr}

    # Delegate to the shared handler with the wrapped subquery
    return _handle_dict_comp(
        modified_filter,
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )


def _handle_if_expr_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle conditional expressions (ternary if-else) in JSONB mode.

    Processes expressions like 'x if condition else y' by:
    1. Evaluating test/then/else branches using JSONB builder
    2. If all branches are direct expressions, use SQL CASE directly
    3. If any branch is a subquery, delegate to shared handler
    """
    from .core import _build_sql_query_jsonb
    from .functions import _handle_if_expr

    # Build all three branches
    test_expr = _build_sql_query_jsonb(
        filter_dict["test"],
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

    body_expr = _build_sql_query_jsonb(
        filter_dict["body"],
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

    else_expr = _build_sql_query_jsonb(
        filter_dict["orelse"],
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

    # Check if all branches are direct expressions (not subqueries)
    all_expressions = (
        _is_jsonb_expression(test_expr)
        and _is_jsonb_expression(body_expr)
        and _is_jsonb_expression(else_expr)
    )

    if all_expressions:
        # Use direct CASE expression for efficiency
        test_type = _infer_expression_type(test_expr, session, project_id, context_id)
        body_type = _infer_expression_type(body_expr, session, project_id, context_id)
        else_type = _infer_expression_type(else_expr, session, project_id, context_id)

        # Unify types for then/else branches
        result_type = unify_inferred_types(body_type, else_type)

        # Cast branches to unified type
        body_casted = cast_expr(body_expr, body_type, result_type)
        else_casted = cast_expr(else_expr, else_type, result_type)

        # Cast test to boolean
        test_bool = cast(test_expr, Boolean) if test_type != "bool" else test_expr

        # Build CASE expression
        return case((test_bool, body_casted), else_=else_casted)

    # If any branch is a subquery, delegate to shared handler
    return _handle_if_expr(
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


def _handle_index_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle the INDEX operator in JSONB mode.

    For JSONB expressions, applies indexing operators directly:
    - Arrays: lhs_expr.op('->')( rhs_expr) for integer index
    - Objects: lhs_expr.op('->')( rhs_expr) for string key
    - Strings: func.substring() with 1-based indexing

    Negative indices are supported for strings and JSONB arrays, following Python semantics.
    Out-of-range indices return NULL (for arrays) or empty string (for strings).

    Returns direct expression when possible, only wraps in subquery if needed.
    """
    from sqlalchemy import BindParameter
    from sqlalchemy.sql.selectable import Subquery

    from .core import _build_sql_query_jsonb
    from .operators import _handle_index_operator

    lhs_node = filter_dict.get("lhs")
    rhs_node = filter_dict.get("rhs")

    # Build LHS and RHS expressions
    lhs_expr = _build_sql_query_jsonb(
        lhs_node,
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

    rhs_expr = _build_sql_query_jsonb(
        rhs_node,
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

    # If LHS is a subquery, delegate to shared handler
    if isinstance(lhs_expr, Subquery):
        return _handle_index_operator(
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

    # Handle JSONB expressions directly
    lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)

    # Get RHS value for indexing
    if isinstance(rhs_expr, BindParameter):
        rhs_val = rhs_expr.value
    elif hasattr(rhs_expr, "value"):
        rhs_val = rhs_expr.value
    else:
        rhs_val = rhs_expr

    # String keys indicate dict property access, not string indexing
    # Even if lhs_type is "str" (default when type is unknown), we should treat
    # string key access as JSONB object property access, not string character indexing.
    # This handles cases like d.a where d is a dict field stored as JSONB.
    is_string_key = isinstance(rhs_val, str)

    # Use centralized type helpers for consistent normalization
    lhs_is_list = _is_list_type(lhs_type) if lhs_type else False
    lhs_is_dict = _is_dict_type(lhs_type) if lhs_type else False

    if lhs_type == "str" and isinstance(rhs_val, int) and not lhs_is_list:
        # String indexing: use func.substring with 1-based index
        # Only apply when RHS is an integer (character index)
        str_val = cast(lhs_expr, String)
        str_len = func.length(str_val)

        if rhs_val < 0:
            # Negative index: compute from end (Python semantics)
            # str_len + rhs_val + 1 gives the 1-based position
            pg_index_expr = str_len + literal(rhs_val) + literal(1)
            return func.substring(str_val, pg_index_expr, 1)
        else:
            # Positive index: convert 0-based to 1-based
            pg_index = rhs_val + 1
            return func.substring(str_val, literal(pg_index), 1)

    elif lhs_is_list or lhs_is_dict or lhs_type == "jsonb" or is_string_key:
        # JSONB array/object indexing using -> operator
        # Note: JSONB -> with negative int does NOT work like Python
        # For lists, we need to handle negative indices specially

        # Ensure LHS is JSONB for -> operator
        # If lhs_expr came from ->> (TEXT), cast it back to JSONB
        from sqlalchemy.sql.expression import BinaryExpression

        lhs_jsonb = lhs_expr
        if isinstance(lhs_expr, BinaryExpression):
            op_str = getattr(lhs_expr.operator, "opstring", str(lhs_expr.operator))
            if op_str == "->>":
                # Cast TEXT back to JSONB for property access
                lhs_jsonb = cast(lhs_expr, JSONB)

        if lhs_is_list and isinstance(rhs_val, int) and rhs_val < 0:
            # Negative index for JSONB array: compute from end
            # jsonb_array_length(arr) + rhs_val gives the actual index
            arr_len = func.jsonb_array_length(cast(lhs_jsonb, JSONB))
            actual_idx = arr_len + literal(rhs_val)
            return lhs_jsonb.op("->")(actual_idx)
        elif isinstance(rhs_val, int):
            return lhs_jsonb.op("->")(literal(rhs_val))
        else:
            return lhs_jsonb.op("->")(rhs_val)

    # Fallback: return null for unsupported types
    return literal(None)


def _handle_slice_operator_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle the SLICE operator in JSONB mode.

    For strings: uses func.substring() with proper 1-based indexing
    For JSONB arrays: uses jsonb_path_query_array for slicing

    Computes start_expr and slice length:
    - lower=None interpreted as 0
    - upper=None interpreted as end of string/array
    - Negative bounds compute from end

    Returns direct expression when possible.
    """
    from sqlalchemy import literal_column
    from sqlalchemy.sql.selectable import Subquery

    from .core import _build_sql_query_jsonb
    from .operators import _handle_slice_operator

    lhs_node = filter_dict.get("lhs")
    rhs_bounds = filter_dict.get("rhs")

    # Unpack the slice bounds
    lower, upper = rhs_bounds

    # Build LHS expression
    lhs_expr = _build_sql_query_jsonb(
        lhs_node,
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

    # If LHS is a subquery, delegate to shared handler
    if isinstance(lhs_expr, Subquery):
        return _handle_slice_operator(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived=is_derived,
            local_scope=local_scope,
            is_vector=is_vector,
        )

    # Handle JSONB expressions directly
    lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)

    # Normalize list types - handle List[int], list, array, etc.
    # Use centralized type helper for consistent normalization
    lhs_is_list = _is_list_type(lhs_type) if lhs_type else False

    if lhs_type == "str" and not lhs_is_list:
        # String slicing using substring
        str_txt = cast(lhs_expr, String)
        str_len = func.char_length(str_txt)

        # Compute start position (1-based for PostgreSQL)
        # lower_index_expr tracks 0-based position for length calculation
        if lower is None:
            start_expr = literal(1)
            lower_index_expr = literal(0)
        elif isinstance(lower, int) and lower >= 0:
            start_expr = literal(lower + 1)  # 1-based
            lower_index_expr = literal(lower)
        elif isinstance(lower, int):  # negative
            start_expr = str_len + literal(lower) + literal(1)
            lower_index_expr = str_len + literal(lower)  # 0-based
        else:
            raise ValueError("Slice start must be int or None")

        # Compute slice length
        if upper is None:
            # No upper bound: go to end of string
            return func.substring(str_txt, start_expr)
        elif isinstance(upper, int) and upper >= 0:
            # Positive upper: compute length as upper - lower
            slice_len = max(
                upper - (lower if lower is not None and lower >= 0 else 0),
                0,
            )
            return func.substring(str_txt, start_expr, literal(slice_len))
        elif isinstance(upper, int):  # negative stop
            # Negative upper: compute end position from string length
            end_index_expr = str_len + literal(upper)
            slice_len_expr = end_index_expr - lower_index_expr
            return func.substring(str_txt, start_expr, slice_len_expr)
        else:
            raise ValueError("Slice stop must be int or None")

    elif lhs_is_list:
        # JSONB array slicing using jsonb_path_query_array with full negative index support
        # Note: JSON path uses 0-based indexing and inclusive 'to'

        # For dynamic negative index handling, we need to compute array length
        arr_len_expr = func.jsonb_array_length(cast(lhs_expr, JSONB))

        # Compute actual start index
        if lower is None:
            start_expr = literal(0)
            start_is_dynamic = False
        elif isinstance(lower, int) and lower >= 0:
            start_expr = literal(lower)
            start_is_dynamic = False
        elif isinstance(lower, int):  # negative start
            # Compute: arr_len + lower, but ensure non-negative
            start_expr = func.greatest(literal(0), arr_len_expr + literal(lower))
            start_is_dynamic = True
        else:
            raise ValueError("Slice start must be int or None")

        # Compute actual end index (inclusive for JSON path)
        if upper is None:
            # Use 'last' keyword in JSON path for unbounded end
            end_is_last = True
            end_expr = None
        elif isinstance(upper, int) and upper >= 0:
            # JSON path 'to' is inclusive, Python slice upper is exclusive
            end_expr = literal(upper - 1)
            end_is_last = False
        elif isinstance(upper, int):  # negative upper
            # Compute: arr_len + upper - 1 (because 'to' is inclusive)
            end_expr = arr_len_expr + literal(upper) - literal(1)
            end_is_last = False
        else:
            raise ValueError("Slice stop must be int or None")

        # For static indices (no negative), use simple path expression
        if not start_is_dynamic and (end_is_last or (upper is not None and upper >= 0)):
            if end_is_last:
                path_expr = f"'$[{lower if lower is not None else 0} to last]'"
            else:
                end_val = upper - 1
                start_val = lower if lower is not None else 0
                if end_val < start_val:
                    # Empty slice - return empty array
                    return func.jsonb_build_array()
                path_expr = f"'$[{start_val} to {end_val}]'"

            return func.jsonb_path_query_array(
                cast(lhs_expr, JSONB),
                literal_column(path_expr),
            )

        # For dynamic indices (negative), we need to use a CASE expression
        # Build dynamic path using string concatenation
        if end_is_last:
            # '$[' || start || ' to last]'
            dynamic_path = func.concat(
                literal("$["),
                start_expr,
                literal(" to last]"),
            )
        else:
            # '$[' || start || ' to ' || end || ']'
            dynamic_path = func.concat(
                literal("$["),
                start_expr,
                literal(" to "),
                end_expr,
                literal("]"),
            )

        # Handle edge case where end < start (empty slice)
        if not end_is_last:
            return case(
                (end_expr < start_expr, func.jsonb_build_array()),
                else_=func.jsonb_path_query_array(
                    cast(lhs_expr, JSONB),
                    dynamic_path.cast(Text),
                ),
            )

        return func.jsonb_path_query_array(
            cast(lhs_expr, JSONB),
            dynamic_path.cast(Text),
        )

    raise ValueError("Slice operation is only supported on string or list values")


def _handle_dict_method_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle dictionary method calls in JSONB mode.

    Supports: .keys(), .values(), .items(), .get(key, default)

    For direct JSONB expressions, wraps in subquery for lateral join operations.
    """
    from sqlalchemy.sql.selectable import Subquery

    from .core import _build_sql_query_jsonb
    from .functions import _handle_dict_method

    method = filter_dict.get("method")

    # Handle .get() specially
    if method in ("get", "setdefault"):
        return _handle_dict_get_jsonb(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope,
            project_id=project_id,
            context_id=context_id,
            default_supplied=filter_dict.get("default_supplied", False),
            query_context=query_context,
        )

    # Build source expression
    src_expr = _build_sql_query_jsonb(
        filter_dict["rhs"],
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

    # If source is already a subquery, delegate to shared handler
    if isinstance(src_expr, Subquery):
        return _handle_dict_method(
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

    # For JSONB expressions, wrap and apply method
    src_type = _infer_expression_type(src_expr, session, project_id, context_id)

    # Use centralized type helper for consistent normalization
    if not _is_dict_type(src_type):
        # Not a dict, return empty list
        return literal([], type_=JSONB)

    # Wrap expression as subquery for lateral join
    src_subq = _wrap_expression_as_subquery(
        src_expr,
        "dict",
        log_event_alias,
        session,
        local_scope=local_scope,
        prefix="dict_method_src",
    )

    # Create a modified filter_dict with the wrapped src subquery
    # The shared handler will use _jsonb_src_subq instead of rebuilding from filter_dict["rhs"]
    modified_filter = {**filter_dict, "_jsonb_src_subq": src_subq}

    # Delegate to shared handler with the wrapped subquery
    return _handle_dict_method(
        modified_filter,
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope=local_scope,
        is_vector=is_vector,
        project_id=project_id,
        context_id=context_id,
    )


def _handle_dict_get_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    default_supplied=None,
    query_context=None,
):
    """
    Handle dict.get(key, default) in JSONB mode.

    Handles dict.get(key, default) with:
    - Accepts default_supplied flag to distinguish .get(key) from .get(key, None)
    - Casts key to String for JSONB -> operator compatibility
    - Uses _select_value and cast_expr to unify types between extracted and default
    - When default_supplied is False, missing keys yield SQL NULL
    """
    from sqlalchemy import BindParameter
    from sqlalchemy.sql.selectable import Subquery

    from orchestra.db.dao.log_dao import LogDAO

    from .core import _build_sql_query_jsonb
    from .functions import _handle_dict_get
    from .helpers import _select_value

    # Use default_supplied from function arg if provided, otherwise from filter_dict
    if default_supplied is None:
        default_supplied = filter_dict.get("default_supplied", False)

    # Build source expression
    src_expr = _build_sql_query_jsonb(
        filter_dict["rhs"],
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

    # If source is a subquery, delegate to shared handler
    if isinstance(src_expr, Subquery):
        return _handle_dict_get(
            filter_dict,
            log_event_alias,
            session,
            log_event_ids,
            is_derived,
            local_scope,
            default_supplied=default_supplied,
            is_vector=is_vector,
            project_id=project_id,
            context_id=context_id,
        )

    # Handle direct JSONB expression
    key_expr = filter_dict.get("key")
    default_expr = filter_dict.get("default")

    # Build key expression
    key = _build_sql_query_jsonb(
        key_expr,
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

    # Get key value - cast to String for JSONB -> operator compatibility
    if isinstance(key, BindParameter):
        key_val = key.value
    elif hasattr(key, "value"):
        key_val = key.value
    else:
        # For non-literal keys, cast to String for -> operator
        key_val = cast(key, String)

    # Ensure src_expr is JSONB for -> operator compatibility
    # If the source was built from ->> (text extraction, e.g., when field type unknown),
    # we need to cast it to JSONB to use the -> operator
    from sqlalchemy.sql.expression import BinaryExpression

    if isinstance(src_expr, BinaryExpression):
        # Check if this is a ->> expression (returns TEXT)
        op_str = getattr(src_expr.operator, "opstring", str(src_expr.operator))
        if op_str == "->>":
            # Cast TEXT back to JSONB for subsequent -> operations
            src_expr = cast(src_expr, JSONB)

    # Extract value using -> operator (returns JSONB, not text)
    extracted = src_expr.op("->")(key_val)

    # If no default supplied, return extracted directly (NULL if key missing)
    # This matches Python's dict.get() behavior
    if not default_supplied or default_expr is None:
        return extracted

    # Build default expression
    default = _build_sql_query_jsonb(
        default_expr,
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

    # Infer types for proper coalescing
    extracted_type = _infer_expression_type(extracted, session, project_id, context_id)

    if isinstance(default, BindParameter):
        default_type = LogDAO.infer_type("", default.value)
    elif isinstance(default, Subquery):
        _, default_type = _select_value(default, session)
    else:
        default_type = _infer_expression_type(default, session, project_id, context_id)

    # Unify types for consistent result
    result_type = unify_inferred_types(extracted_type, default_type)

    # Cast both to unified type before coalescing
    extracted_casted = cast_expr(extracted, extracted_type, result_type)
    default_casted = cast_expr(default, default_type, result_type)

    return func.coalesce(extracted_casted, default_casted)


def _handle_str_method_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle string method calls in JSONB mode.

    Maps Python string methods to PostgreSQL functions:
    - .lower() → func.lower()
    - .upper() → func.upper()
    - .strip() → func.trim()
    - .startswith(prefix) → str_expr.like(prefix || '%')
    - .endswith(suffix) → str_expr.like('%' || suffix)
    - .contains(substr) → position() > 0
    - .replace(old, new) → func.replace()
    - .split(delim) → func.string_to_array()

    Returns direct expression for efficiency.
    """
    from sqlalchemy import BindParameter
    from sqlalchemy.sql.selectable import Subquery

    from .core import _build_sql_query_jsonb
    from .functions import _handle_str_method

    method = filter_dict.get("method")
    bool_methods = {"startswith", "endswith", "contains", "match"}

    # Build source expression
    src_expr = _build_sql_query_jsonb(
        filter_dict["rhs"],
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

    # If source is a subquery, delegate to shared handler
    if isinstance(src_expr, Subquery):
        return _handle_str_method(
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

    # Build argument expressions
    args = []
    if "args" in filter_dict and filter_dict["args"]:
        args = [
            _build_sql_query_jsonb(
                arg,
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
            for arg in filter_dict["args"]
        ]

    # Cast source to string
    # For JSONB expressions with -> operator, use .astext to strip JSON quotes
    # Check if expression has .astext attribute (e.g., BinaryExpression with -> op)
    if _is_jsonb_expression(src_expr) and hasattr(src_expr, "astext"):
        str_val = src_expr.astext
    else:
        str_val = cast(src_expr, String)

    # Apply the appropriate string operation
    if method == "lower":
        return func.lower(str_val)
    elif method == "upper":
        return func.upper(str_val)
    elif method == "capitalize":
        return func.concat(
            func.upper(func.substr(str_val, 1, 1)),
            func.lower(func.substr(str_val, 2)),
        )
    elif method == "strip":
        if args:
            chars = cast(args[0], String)
            return func.btrim(str_val, chars)
        else:
            return func.trim(str_val)
    elif method == "lstrip":
        if args:
            chars = cast(args[0], String)
            return func.ltrim(str_val, chars)
        else:
            return func.ltrim(str_val)
    elif method == "rstrip":
        if args:
            chars = cast(args[0], String)
            return func.rtrim(str_val, chars)
        else:
            return func.rtrim(str_val)
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
            return func.strpos(str_val, substring_val) > 0
        else:
            return func.strpos(str_val, substring) > 0
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
    elif method == "split":
        # Wrap string_to_array in to_jsonb to ensure 0-based indexing with ->
        delim = args[0] if args else literal(" ")
        return func.to_jsonb(func.string_to_array(str_val, delim))
    elif method == "substring":
        # substring(start, end) -> func.substr(str, start, length)
        # Python slice semantics: start is 0-based, end is exclusive.
        # SQL substr: start is 1-based, length.
        # But the parser might be passing arguments differently.
        # Based on test: s.substring(1, 5) == 'hello' (where s='hello world')
        # This implies start=1 (1-based), length=5? Or end=5?
        # 'hello' is 5 chars.
        # If it's python slice [1:5], it would be 'ello'.
        # If it's SQL substring(1, 5), it's 'hello'.
        # The test comment says "SQL 1-based".
        # So we assume args are (start, length) or (start, end).
        # Let's assume (start, length) as per standard SQL substring.
        if len(args) == 1:
            return func.substr(str_val, cast(args[0], Integer))
        elif len(args) == 2:
            return func.substr(str_val, cast(args[0], Integer), cast(args[1], Integer))
        else:
            raise ValueError("substring() requires 1 or 2 arguments")
    else:
        raise ValueError(f"Unsupported string method in JSONB mode: {method}")


def _handle_zip_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle zip() function in JSONB mode.
    Wraps arguments in subqueries and performs the join logic.
    """
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.sql.selectable import Subquery

    from . import alias_utils
    from .core import _build_sql_query_jsonb
    from .helpers import _get_parent_idx, _select_value

    # Build and wrap arguments
    zipped_subqs = []
    for idx, arg in enumerate(filter_dict["rhs"]):
        # Build the expression
        expr = _build_sql_query_jsonb(
            arg,
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

        # Wrap if not already a subquery
        if not isinstance(expr, Subquery):
            inferred_type = _infer_expression_type(
                expr,
                session,
                project_id,
                context_id,
            )
            expr = _wrap_expression_as_subquery(
                expr,
                inferred_type,
                log_event_alias,
                session,
                local_scope=local_scope,
                prefix=f"zip_arg_{idx}",
            )

        # Now we have a subquery, prepare it for joining
        col, _ = _select_value(expr, session, is_collection=True)
        # Ensure col is JSONB (it might be Text if coming from ->>)
        col = cast(col, JSONB)
        parent_idx_col = _get_parent_idx(expr.c)

        # Unnest the array
        table_valued = (
            func.jsonb_array_elements(col)
            .table_valued("value", with_ordinality="ordinality")
            .alias(f"elem_tbl_{idx}")
        )

        sub_cols = [
            expr.c.log_event_id.label("log_event_id"),
            table_valued.c.ordinality.label("ordinality"),
            table_valued.c.value.label(f"value_{idx}"),
        ]
        if parent_idx_col is not None:
            sub_cols.append(parent_idx_col.label("__parent_idx__"))

        sub = alias_utils.subquery_with_unique_alias(
            select(*sub_cols).select_from(expr.join(table_valued, literal(True))),
            prefix=f"zip_subq_{idx}",
        )
        zipped_subqs.append(sub)

    # Join the subqueries
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
            func.jsonb_agg(func.jsonb_build_array(*value_columns)),
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
    # Return the subquery directly (not scalar_subquery!)
    # scalar_subquery() fails with CardinalityViolation when there are multiple log events
    return zipped


def _handle_embed_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle embed() function in JSONB mode.

    embed(text, model?, dimensions?) - Converts text to a vector embedding

    In JSONB mode:
    - LogEvent.data contains all fields as JSONB
    - Embedding table stores computed vectors
    - NO Log/DerivedLog tables

    Handles two cases:
    1. Literal text: Create embedding directly and return vector literal
    2. Field references: Extract text from LogEvent.data, create embeddings, return from Embedding table
    """
    from .core import _build_sql_query_jsonb
    from .helpers import _queue_embeddings_for_generation

    rhs_dict = filter_dict.get("rhs")
    if not isinstance(rhs_dict, list):
        rhs_dict = [rhs_dict]

    if len(rhs_dict) < 1 or len(rhs_dict) > 3:
        raise ValueError(
            "embed() requires 1-3 arguments: (text, [model], [dimensions])",
        )

    # Build expressions for each argument
    text_expr = _build_sql_query_jsonb(
        rhs_dict[0],
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

    # Process optional model parameter
    model = None
    if len(rhs_dict) >= 2:
        model_expr = _build_sql_query_jsonb(
            rhs_dict[1],
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
        if isinstance(model_expr, BindParameter):
            model = model_expr.value
            if not isinstance(model, str):
                raise ValueError(
                    f"embed() model must be a string, got {type(model).__name__}",
                )
        elif hasattr(model_expr, "value"):
            model = model_expr.value

    # Process optional dimensions parameter
    dimensions = None
    if len(rhs_dict) == 3:
        dim_expr = _build_sql_query_jsonb(
            rhs_dict[2],
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
        if isinstance(dim_expr, BindParameter):
            dimensions = dim_expr.value
            if not isinstance(dimensions, int):
                raise ValueError(
                    f"embed() dimensions must be an integer, got {type(dimensions).__name__}",
                )
        elif hasattr(dim_expr, "value"):
            dimensions = dim_expr.value

    # Case 1: Handle literal text values (BindParameter)
    if isinstance(text_expr, BindParameter):
        text = text_expr.value
        if not isinstance(text, str):
            raise ValueError(f"embed() requires a string, got {type(text).__name__}")

        if not _embeddable(text):
            raise ValueError(f"embed() requires a valid embeddable string, got {text}")

        # Get the embedding vector
        embedding = _get_embedding(text, model, dimensions)

        # Create a vector literal using pgvector
        return literal(embedding, type_=Vector(len(embedding)))

    # Case 2: Handle field references - JSONB native approach
    # Get the key from the original filter dict
    first_arg = rhs_dict[0]
    key = None
    if isinstance(first_arg, dict) and first_arg.get("type") == "identifier":
        key = first_arg["value"]
    elif (
        isinstance(first_arg, dict)
        and first_arg.get("operand") == "BASE"
        and len(first_arg.get("rhs", [])) >= 2
        and isinstance(first_arg["rhs"][1], dict)
        and first_arg["rhs"][1].get("type") == "identifier"
    ):
        key = first_arg["rhs"][1]["value"]

    if key is None:
        raise ValueError("embed(): could not resolve key from first argument")

    # JSONB-native: Query LogEvent.data directly to get text values
    # Build query: SELECT id, data->>'key' FROM log_event WHERE id IN (log_event_ids)
    text_query = select(
        log_event_alias.id.label("log_event_id"),
        log_event_alias.data.op("->>")(key).label("text_value"),
    ).select_from(log_event_alias)

    # Filter by log_event_ids to scope query to user's project/context
    if log_event_ids is not None:
        text_query = text_query.where(
            log_event_alias.id.in_(select(log_event_ids.c.id)),
        )

    # Execute to get id_to_text mapping
    rows = session.execute(text_query).fetchall()
    id_to_text = {}
    for row in rows:
        if row.text_value and isinstance(row.text_value, str):
            id_to_text[row.log_event_id] = row.text_value

    # Generate embeddings: async (queued) or sync (immediate)
    # Controlled by async_embeddings kwarg in embed() call (defaults to False = sync)
    if id_to_text:
        async_embeddings = filter_dict.get("async_embeddings", False)

        if async_embeddings:
            # Async: queue for background generation
            _queue_embeddings_for_generation(
                session=session,
                id_to_text=id_to_text,
                model=model,
                dimensions=dimensions,
                key=key,
            )
        else:
            # Sync: generate embeddings immediately
            from .helpers import _ensure_vectors_exist

            _ensure_vectors_exist(
                session=session,
                id_to_text=id_to_text,
                model=model,
                dimensions=dimensions,
                key=key,
            )

    # JSONB-native: Build subquery joining LogEvent with Embedding table
    # to return vectors for each log_event_id
    model_name = model or "text-embedding-3-small"  # Default model

    vector_subq = (
        select(
            log_event_ids.c.id.label("log_event_id"),
            Embedding.vector.label("value"),
            literal("vector").label("inferred_type"),
        )
        .select_from(log_event_ids)
        .outerjoin(
            Embedding,
            and_(
                Embedding.ref_id == log_event_ids.c.id,
                Embedding.key == literal(key),
                Embedding.model == literal(model_name),
            ),
        )
    )

    return alias_utils.subquery_with_unique_alias(
        vector_subq,
        prefix="embed_result",
    )


def _handle_embed_image_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle embed_image() function in JSONB mode.

    embed_image(image_url_or_base64) - Converts image to vector embedding

    In JSONB mode, image URLs are stored in LogEvent.data and we compute
    embeddings on-the-fly (not stored in Embedding table since they're image-based).

    Handles three cases:
    1. Subquery: Execute query, compute embeddings in parallel
    2. JSONB expression (field reference): Query LogEvent.data, compute embeddings
    3. Literal (base64/URL): Compute embedding directly
    """
    from .core import _build_sql_query_jsonb

    rhs_dict = filter_dict.get("rhs")

    # Build expression for the argument
    image_expr = _build_sql_query_jsonb(
        rhs_dict,
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

    # Helper to process rows and compute embeddings
    def _compute_embeddings_for_rows(rows):
        if not rows:
            return None

        bucket_service = BucketService()

        def compute_image_embedding(args):
            log_event_id, image_url, bucket_svc = args
            embedding_vector = None
            error_msg = None

            # Handle image dict format (e.g., {'type': 'image', 'value': 'data:...'})
            if isinstance(image_url, dict) and image_url.get("type") == "image":
                image_url = image_url.get("value")

            if image_url and isinstance(image_url, str):
                embedding_vector = _get_image_embedding_from_url(image_url, bucket_svc)
                if embedding_vector is None:
                    error_msg = (
                        f"Failed to compute embedding for log_event_id={log_event_id}"
                    )
            return {
                "log_event_id": log_event_id,
                "value": embedding_vector,
                "error": error_msg,
            }

        results = threaded_map(
            compute_image_embedding,
            [
                (log_event_id, image_url, bucket_service)
                for log_event_id, image_url in rows
            ],
        )

        failed_count = sum(1 for r in results if r["value"] is None)
        success_count = len(results) - failed_count

        if failed_count > 0:
            logging.warning(
                f"embed_image: {failed_count}/{len(results)} images failed. "
                f"Successfully processed {success_count}/{len(results)} images.",
            )

        selects = [
            select(
                literal(r["log_event_id"]).label("log_event_id"),
                _literal_vector_jsonb(r["value"], len(r["value"])).label("value"),
                literal("vector").label("inferred_type"),
            )
            for r in results
            if r["value"] is not None
        ]

        if not selects:
            logging.error(f"embed_image: All {len(results)} image embeddings failed!")
            return None

        return alias_utils.subquery_with_unique_alias(
            union_all(*selects).select(),
            prefix="embed_image_result",
        )

    # Case 1: Handle Subquery
    if isinstance(image_expr, Subquery):
        rows = session.execute(
            select(image_expr.c.log_event_id, image_expr.c.value),
        ).fetchall()
        return _compute_embeddings_for_rows(rows)

    # Case 2: Handle BindParameter (literal base64 strings or GCS URLs)
    # Check BindParameter before _is_jsonb_expression (BindParameter returns True but needs special handling)
    elif isinstance(image_expr, BindParameter):
        image_value = image_expr.value

        # Handle image type dict (from parser: {"type": "image", "value": "data:..."})
        if isinstance(image_value, dict) and image_value.get("type") == "image":
            image_string = image_value.get("value")
        else:
            image_string = image_value

        if not isinstance(image_string, str):
            raise ValueError("embed_image() requires a string URL or base64 image")

        embedding = _get_image_embedding_from_url(image_string)
        if embedding is None:
            raise RuntimeError("Failed to generate image embedding")

        return literal(embedding, type_=Vector(len(embedding)))

    # Case 3: Handle JSONB expression (field reference)
    # In JSONB mode, identifiers return JSONB expressions, not subqueries
    elif _is_jsonb_expression(image_expr):
        # Query LogEvent.data directly to get image URLs
        # Filter by log_event_ids to only process relevant logs
        query = select(
            log_event_alias.id.label("log_event_id"),
            image_expr.label("value"),
        ).select_from(log_event_alias)

        # Apply log_event_ids filter if provided
        if log_event_ids is not None:
            query = query.where(log_event_alias.id.in_(select(log_event_ids)))

        rows = session.execute(query).fetchall()
        return _compute_embeddings_for_rows(rows)

    else:
        raise ValueError(
            "embed_image() expects a GCS URL, base64 string, or a field reference.",
        )


def _handle_phash_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle phash() function in JSONB mode.

    phash(image_url) - Converts image URL to a perceptual hash hex string

    In JSONB mode, image URLs are stored in LogEvent.data and we compute
    perceptual hashes on-the-fly.

    Handles three cases:
    1. Subquery: Execute query, compute hashes in parallel
    2. JSONB expression (field reference): Query LogEvent.data, compute hashes
    3. Literal (URL): Compute hash directly
    """
    from .core import _build_sql_query_jsonb

    rhs_dict = filter_dict.get("rhs")

    # Build expression for the argument
    image_expr = _build_sql_query_jsonb(
        rhs_dict,
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

    # Helper to process rows and compute hashes
    def _compute_hashes_for_rows(rows):
        if not rows:
            return None

        bucket_service = BucketService()

        def compute_image_hash(args):
            log_event_id, image_url, bucket_svc = args
            phash_hex = None
            error_msg = None

            # Handle image dict format (e.g., {'type': 'image', 'value': 'data:...'})
            if isinstance(image_url, dict) and image_url.get("type") == "image":
                image_url = image_url.get("value")

            if image_url and isinstance(image_url, str):
                try:
                    # Check if this is a base64 data URI or a GCS URL
                    if image_url.startswith("data:image/") and ";base64," in image_url:
                        # Base64 data URI - extract and decode directly
                        b64_string = image_url.split(",", 1)[1]
                        image_data = base64.b64decode(b64_string)
                    else:
                        # GCS URL - fetch from bucket service
                        base64_image = bucket_svc.get_media(image_url.split("/")[-1])
                        if not base64_image:
                            error_msg = (
                                f"Failed to fetch image for log_event_id={log_event_id}"
                            )
                            return {
                                "log_event_id": log_event_id,
                                "value": None,
                                "error": error_msg,
                            }
                        image_data = base64.b64decode(base64_image)

                    image = Image.open(io.BytesIO(image_data))
                    hash_value = imagehash.phash(image)
                    phash_hex = format(int(str(hash_value), 16), "016x")
                except Exception as e:
                    error_msg = (
                        f"Failed to compute phash for log_event_id={log_event_id}: {e}"
                    )
            return {
                "log_event_id": log_event_id,
                "value": phash_hex,
                "error": error_msg,
            }

        results = threaded_map(
            compute_image_hash,
            [
                (log_event_id, image_url, bucket_service)
                for log_event_id, image_url in rows
            ],
        )

        failed_count = sum(1 for r in results if r["value"] is None)
        success_count = len(results) - failed_count

        if failed_count > 0:
            logging.warning(
                f"phash: {failed_count}/{len(results)} images failed. "
                f"Successfully processed {success_count}/{len(results)} images.",
            )

        selects = [
            select(
                literal(r["log_event_id"]).label("log_event_id"),
                literal(r["value"]).label("value"),
                literal("str").label("inferred_type"),
            )
            for r in results
            if r["value"] is not None
        ]

        if not selects:
            logging.error(f"phash: All {len(results)} image hashes failed!")
            return None

        return alias_utils.subquery_with_unique_alias(
            union_all(*selects).select(),
            prefix="phash_result",
        )

    # Case 1: Handle Subquery
    if isinstance(image_expr, Subquery):
        rows = session.execute(
            select(image_expr.c.log_event_id, image_expr.c.value),
        ).fetchall()
        return _compute_hashes_for_rows(rows)

    # Case 2: Handle BindParameter (literal URL or image dict)
    # Check BindParameter before _is_jsonb_expression (BindParameter returns True but needs special handling)
    elif isinstance(image_expr, BindParameter):
        image_value = image_expr.value

        # Handle image type dict (from parser: {"type": "image", "value": "data:..."})
        if isinstance(image_value, dict) and image_value.get("type") == "image":
            image_url = image_value.get("value")
        elif isinstance(image_value, str):
            image_url = image_value
        else:
            raise ValueError("phash() requires a string URL or base64 image")

        try:
            # Check if this is a base64 data URI or a GCS URL
            if image_url.startswith("data:image/") and ";base64," in image_url:
                # Base64 data URI - extract and decode directly
                b64_string = image_url.split(",", 1)[1]
                image_data = base64.b64decode(b64_string)
            else:
                # GCS URL - fetch from bucket service
                bucket_service = BucketService()
                base64_image = bucket_service.get_media(image_url.split("/")[-1])
                if not base64_image:
                    return literal(None)
                image_data = base64.b64decode(base64_image)

            image = Image.open(io.BytesIO(image_data))
            hash_value = imagehash.phash(image)
            return literal(format(int(str(hash_value), 16), "016x"))
        except Exception:
            return literal(None)

    # Case 3: Handle JSONB expression (field reference)
    # In JSONB mode, identifiers return JSONB expressions, not subqueries
    elif _is_jsonb_expression(image_expr):
        # Query LogEvent.data directly to get image URLs
        query = select(
            log_event_alias.id.label("log_event_id"),
            image_expr.label("value"),
        ).select_from(log_event_alias)

        # Filter by log_event_ids to scope query to user's project/context
        if log_event_ids is not None:
            query = query.where(log_event_alias.id.in_(select(log_event_ids.c.id)))

        rows = session.execute(query).fetchall()
        return _compute_hashes_for_rows(rows)

    else:
        raise ValueError("phash() expects a string URL or a field reference")


def _ensure_numeric_iterable_jsonb(name: str, val: Iterable) -> Tuple[list, int]:
    """Validate and convert iterables to float lists for vector operations."""
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


def _literal_vector_jsonb(vec: Iterable[float], dim: int) -> ClauseElement:
    """Create pgvector literal from Python list."""
    return cast(literal(list(vec), type_=ARRAY(Float())), Vector(dim))


def _coerce_to_vector_sql_jsonb(
    expr: object,
    inferred_type: Optional[str],
    side_label: str,
) -> ClauseElement:
    """Convert various types to Vector SQL expressions."""
    if hasattr(expr, "op"):
        if _is_list_type(inferred_type):
            return cast(expr.op("#>>")("{}"), Vector())
        return expr
    if _is_list_type(inferred_type) and isinstance(expr, (list, tuple)):
        vec, dim = _ensure_numeric_iterable_jsonb(side_label, expr)
        return _literal_vector_jsonb(vec, dim)
    if isinstance(expr, (list, tuple)):
        vec, dim = _ensure_numeric_iterable_jsonb(side_label, expr)
        return _literal_vector_jsonb(vec, dim)
    raise TypeError(
        f"Vector operand {side_label}: expected a vector-compatible value "
        f"(numeric list/tuple or SQL expression), got {type(expr).__name__}.",
    )


def _vector_binary_op_jsonb(
    lhs_src: Union[ClauseElement, Subquery, object],
    rhs_src: Union[ClauseElement, Subquery, object],
    session,
    operator_symbol: str,
    result_type_label: str,
    subquery_prefix: str,
) -> Union[ClauseElement, Subquery]:
    """
    Apply a binary vector operator (e.g., <->, <=>, <#>, <+>, <%>) between two operands.

    Applies binary vector operators between two operands in JSONB mode.
    """
    lhs_is_sub = isinstance(lhs_src, Subquery)
    rhs_is_sub = isinstance(rhs_src, Subquery)

    def _value_from_source(src, side_name: str):
        val, val_type = _select_value(src, session, is_vector=True)
        return (
            _coerce_to_vector_sql_jsonb(val, val_type, side_name),
            val_type,
            (src if isinstance(src, Subquery) else None),
        )

    lval, _, lsub = _value_from_source(lhs_src, "lhs")
    rval, _, rsub = _value_from_source(rhs_src, "rhs")

    expr = lval.op(operator_symbol)(rval).cast(Float)

    # Both sides are subqueries
    if lsub is not None and rsub is not None:
        return _join_subqueries(lsub, rsub, expr, result_type_label, session=session)

    # Only LHS is subquery
    if lsub is not None:
        return build_result_subquery(
            base_subq=lsub,
            value_expr=expr,
            result_type=result_type_label,
            prefix=subquery_prefix,
        )

    # Only RHS is subquery
    if rsub is not None:
        return build_result_subquery(
            base_subq=rsub,
            value_expr=expr,
            result_type=result_type_label,
            prefix=subquery_prefix,
        )

    # Neither side is subquery
    return expr


def _handle_l2_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle L2/Euclidean distance operator between two vectors: v1 <-> v2."""
    from .core import _build_sql_query_jsonb

    lhs = _build_sql_query_jsonb(
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
    rhs = _build_sql_query_jsonb(
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
    return _vector_binary_op_jsonb(lhs, rhs, session, "<->", "float", "l2_distance")


def _handle_cosine_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle cosine similarity operator between two vectors: v1 <=> v2."""
    from .core import _build_sql_query_jsonb

    lhs = _build_sql_query_jsonb(
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
    rhs = _build_sql_query_jsonb(
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
    return _vector_binary_op_jsonb(
        lhs,
        rhs,
        session,
        "<=>",
        "float",
        "cosine_similarity",
    )


def _handle_ip_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle inner product operator between two vectors: v1 <#> v2."""
    from .core import _build_sql_query_jsonb

    lhs = _build_sql_query_jsonb(
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
    rhs = _build_sql_query_jsonb(
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
    return _vector_binary_op_jsonb(lhs, rhs, session, "<#>", "float", "inner_product")


def _handle_l1_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle L1/Manhattan distance operator between two vectors: v1 <+> v2."""
    from .core import _build_sql_query_jsonb

    lhs = _build_sql_query_jsonb(
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
    rhs = _build_sql_query_jsonb(
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
    return _vector_binary_op_jsonb(lhs, rhs, session, "<+>", "float", "l1_distance")


def _handle_euclidean_distance_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Alias for L2 distance."""
    return _handle_l2_jsonb(
        filter_dict,
        log_event_alias,
        session,
        log_event_ids,
        is_derived,
        local_scope,
        is_vector,
        project_id,
        context_id,
        query_context,
    )


def _handle_jaccard_distance_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """Handle Jaccard distance operator between two vectors: v1 <%> v2."""
    from .core import _build_sql_query_jsonb

    lhs = _build_sql_query_jsonb(
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
    rhs = _build_sql_query_jsonb(
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
    return _vector_binary_op_jsonb(
        lhs,
        rhs,
        session,
        "<%>",
        "float",
        "jaccard_distance",
    )


def _handle_phash_distance_jsonb(
    filter_dict,
    log_event_alias,
    session,
    log_event_ids,
    is_derived=False,
    local_scope=None,
    is_vector=False,
    project_id=None,
    context_id=None,
    query_context=None,
):
    """
    Handle Hamming distance operator between two pHash hex strings.

    Uses PostgreSQL's hamming_distance function to compute the distance.
    """
    from .core import _build_sql_query_jsonb

    lhs_dict = filter_dict.get("lhs")
    rhs_dict = filter_dict.get("rhs")

    # Check for raw image literals and compute their pHash on the fly
    # Use image_utils for pHash computation
    lhs_phash = get_phash_from_node(lhs_dict)
    if lhs_phash is not None:
        lhs = literal(lhs_phash)
    else:
        lhs = _build_sql_query_jsonb(
            lhs_dict,
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

    rhs_phash = get_phash_from_node(rhs_dict)
    if rhs_phash is not None:
        rhs = literal(rhs_phash)
    else:
        rhs = _build_sql_query_jsonb(
            rhs_dict,
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

    lhs_is_sub = isinstance(lhs, Subquery)
    rhs_is_sub = isinstance(rhs, Subquery)

    if lhs_is_sub and rhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)
        # Convert hex phash strings to bit(64) for hamming_distance
        lval_bit = cast(func.concat(literal("x"), lval), Bit(64))
        rval_bit = cast(func.concat(literal("x"), rval), Bit(64))
        expr = func.hamming_distance(lval_bit, rval_bit)
        return _join_subqueries(lhs, rhs, expr, "int", session=session)

    if lhs_is_sub:
        lval, _ = _select_value(lhs, session)
        rval, _ = _select_value(rhs, session)

        # For BindParameter/literal rhs, need to convert to SQL literal
        # _select_value returns Python value for BindParameter, we need SQL expression
        if isinstance(rhs, BindParameter) or isinstance(rval, str):
            rval = literal(rval)

        # Convert hex phash strings to bit(64) for hamming_distance
        lval_bit = cast(func.concat(literal("x"), lval), Bit(64))
        rval_bit = cast(func.concat(literal("x"), rval), Bit(64))
        expr = func.hamming_distance(lval_bit, rval_bit)

        return build_result_subquery(
            base_subq=lhs,
            value_expr=expr,
            result_type="int",
            prefix="phash_distance",
        )

    if rhs_is_sub:
        rval, _ = _select_value(rhs, session)
        lval, _ = _select_value(lhs, session)

        # For BindParameter/literal lhs, need to convert to SQL literal
        if isinstance(lhs, BindParameter) or isinstance(lval, str):
            lval = literal(lval)

        # Convert hex phash strings to bit(64) for hamming_distance
        lval_bit = cast(func.concat(literal("x"), lval), Bit(64))
        rval_bit = cast(func.concat(literal("x"), rval), Bit(64))
        expr = func.hamming_distance(lval_bit, rval_bit)

        return build_result_subquery(
            base_subq=rhs,
            value_expr=expr,
            result_type="int",
            prefix="phash_distance",
        )

    # Neither side is a subquery - return scalar expression
    # Convert hex phash strings to bit(64) for hamming_distance
    # hamming_distance expects bit strings, not hex strings
    # Use ('x' || hex_string)::bit(64) to convert hex to binary
    lhs_bit = cast(func.concat(literal("x"), lhs), Bit(64))
    rhs_bit = cast(func.concat(literal("x"), rhs), Bit(64))
    return func.hamming_distance(lhs_bit, rhs_bit)
