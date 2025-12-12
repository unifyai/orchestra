"""
Query builder for JSONB-based log storage. Translates Python filter expressions to PostgreSQL JSONB queries.
"""

import base64
import io
import logging
from typing import Any, Iterable, Optional, Tuple, Union

import imagehash
import unify
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


def _build_truthiness_sql(val_col, val_type):
    """
    Build SQL truthiness condition based on value type.
    """
    normalized_type = _normalize_type(val_type)

    if normalized_type == "bool":
        # Cast to boolean and check if True
        return cast(val_col, Boolean).is_(True)
    elif normalized_type in ("int", "float"):
        # For numbers, check if not 0 and not null
        return and_(val_col.isnot(None), cast(val_col, Float) != 0)
    elif normalized_type == "str":
        # For strings, check if not empty and not null
        return and_(val_col.isnot(None), func.length(cast(val_col, String)) > 0)
    elif normalized_type == "list":
        # For lists, check if not empty
        return func.jsonb_array_length(val_col) > 0
    elif normalized_type == "dict":
        # For dicts, check if not empty
        return val_col != cast(literal("{}"), JSONB)
    elif val_type == "NoneType":
        return literal(False)
    else:
        # For other types, check if not null
        return val_col.isnot(None)


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

        vector_subq = (
            select(
                log_event_alias.id.label("log_event_id"),
                Embedding.vector.label("value"),
                literal("vector").label("inferred_type"),
            )
            .select_from(log_event_alias)
            .outerjoin(
                Embedding,
                and_(
                    Embedding.ref_id == log_event_alias.id,
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
    field_type = _get_field_type_from_db(key, session, project_id, context_id)

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
                        # Verify field type is compatible with containment operator
                        # Containment works for: int, float, str, bool, NoneType
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
                            "NoneType",
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
                            # jsonb_build_object preserves Python types (int/float→number, str→string, bool→boolean, None→null)
                            containment_check = log_event_alias.data.op("@>")(
                                func.jsonb_build_object(lhs_field_name, rhs_value),
                            )
                            return containment_check

        # Fall through to existing logic for complex cases
        # (nested access, arithmetic, functions, non-equality operators)

        lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)
        rhs_type = _infer_expression_type(rhs_expr, session, project_id, context_id)

        # Special handling for `is None` / `is not None` in JSONB mode
        # JSONB null values can be either SQL NULL or the JSONB literal "null"
        # We need to check for both cases to properly match None comparisons
        if operand in ("is", "is not") and rhs_type == "NoneType":
            # Cast LHS to text for comparison (handles both SQL NULL and JSONB "null")
            lhs_as_text = cast(lhs_expr, Text)
            if operand == "is":
                # `field is None` → field is SQL NULL OR field equals the string "null"
                return or_(lhs_as_text.is_(None), lhs_as_text == "null")
            else:
                # `field is not None` → field is NOT SQL NULL AND field is not "null"
                return and_(lhs_as_text.isnot(None), lhs_as_text != "null")

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
    is_in = operand == "in"

    # If both are JSONB expressions (common case)
    if not lhs_is_sub and not rhs_is_sub:
        lhs_type = _infer_expression_type(lhs_expr, session, project_id, context_id)
        rhs_type = _infer_expression_type(rhs_expr, session, project_id, context_id)

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
                return lhs_expr.in_(rhs_expr) if is_in else not_(lhs_expr.in_(rhs_expr))
            elif hasattr(rhs_expr, "value"):
                rhs_list = rhs_expr.value
            if rhs_list is not None:
                return lhs_expr.in_(rhs_list) if is_in else not_(lhs_expr.in_(rhs_list))

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

