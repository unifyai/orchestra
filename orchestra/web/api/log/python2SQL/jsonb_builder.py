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