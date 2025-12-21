"""
Shared truthiness logic for Python-to-SQL filter translation.

This module provides unified truthiness handling for both EAV and JSONB modes,
implementing Python's truthiness semantics in SQL:
    - None/null: False
    - False: False
    - 0, 0.0: False
    - "" (empty string): False
    - [] (empty list): False
    - {} (empty dict): False
    - Everything else: True
"""

from sqlalchemy import Boolean, Float, and_, case, cast, func, literal
from sqlalchemy.dialects.postgresql import JSONB

from .type_mapping import normalize_type


def build_truthiness_sql(val_col, val_type):
    """
    Build SQL truthiness condition based on value type.

    Implements Python truthiness semantics for SQL expressions. Handles all
    standard Python types plus JSONB with runtime type checking.

    Args:
        val_col: SQLAlchemy column or expression to evaluate
        val_type: Type string (e.g., "bool", "int", "str", "list", "dict", "jsonb")

    Returns:
        SQLAlchemy expression that evaluates to True/False based on Python truthiness
    """
    normalized_type = normalize_type(val_type)

    # Extract text representation from JSONB for scalar types.
    # #>> '{}' extracts the root value as text, avoiding JSON quoting issues.
    val_as_text = val_col.op("#>>")(literal("{}"))

    if normalized_type == "bool":
        # Cast to boolean and check if True
        # For JSONB, casting works correctly for boolean values
        return cast(val_col, Boolean).is_(True)
    elif normalized_type in ("int", "float"):
        # For numbers, extract as text and cast to float for comparison
        # Using #>> '{}' to get raw text avoids JSON quoting
        return and_(val_col.isnot(None), cast(val_as_text, Float) != 0)
    elif normalized_type == "str":
        # For strings, extract as text and check length
        # Using #>> '{}' extracts the actual string content without JSON quotes
        return and_(val_col.isnot(None), func.length(val_as_text) > 0)
    elif normalized_type == "list":
        # For lists, check if not empty
        return func.jsonb_array_length(val_col) > 0
    elif normalized_type == "dict":
        # For dicts, check if not empty
        return val_col != cast(literal("{}"), JSONB)
    elif normalized_type == "jsonb":
        # For generic JSONB, we need runtime type checking since it could be
        # any JSON type: object, array, string, number, boolean, or null.
        # Use jsonb_typeof() to determine the type at runtime and apply
        # appropriate truthiness rules.
        jsonb_type = func.jsonb_typeof(val_col)
        # Cast to text for string/number extraction (works on JSONB columns)
        # Use #>> '{}' to extract text representation from JSONB
        val_as_text = val_col.op("#>>")(literal("{}"))
        return and_(
            val_col.isnot(None),  # SQL NULL check
            jsonb_type != literal("null"),  # JSON null check
            case(
                # Empty object is falsy
                (
                    jsonb_type == literal("object"),
                    val_col != cast(literal("{}"), JSONB),
                ),
                # Empty array is falsy
                (jsonb_type == literal("array"), func.jsonb_array_length(val_col) > 0),
                # Empty string is falsy
                (jsonb_type == literal("string"), func.length(val_as_text) > 0),
                # Number 0 is falsy
                (jsonb_type == literal("number"), cast(val_as_text, Float) != 0),
                # Boolean: use the value itself
                (jsonb_type == literal("boolean"), cast(val_col, Boolean).is_(True)),
                # For any other type, truthy if not null (already checked above)
                else_=literal(True),
            ),
        )
    elif val_type == "NoneType":
        return literal(False)
    else:
        # For other types (timestamp, etc.), check if not null
        return val_col.isnot(None)


def get_or_list_fallback(node_dict):
    """
    Check if a node represents the pattern (expr or <list>).

    This is a common Python idiom for safe iteration over potentially null arrays:
        'x' in (arr or [])
        'x' in (arr or ['default'])

    Python's `or` operator returns one of its operands, not a boolean.
    So `arr or []` returns `arr` if truthy, else `[]`.

    Args:
        node_dict: Parsed filter expression dictionary

    Returns:
        The fallback list if the pattern matches, None otherwise.
    """
    if (
        isinstance(node_dict, dict)
        and node_dict.get("operand") == "or"
        and isinstance(node_dict.get("rhs"), list)
    ):
        return node_dict.get("rhs")
    return None
