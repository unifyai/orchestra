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

