"""
Type mapping utilities for normalizing FieldType strings.

This module consolidates type normalization logic that was previously
duplicated across multiple locations in jsonb_builder.py.
"""


__all__ = [
    "TYPE_NORMALIZATION",
    "normalize_field_type",
    "normalize_type",
    "get_python_type_literal",
    "is_collection_type",
    "is_numeric_type",
    "is_temporal_type",
]


# Mapping from various type names to normalized Python type names
TYPE_NORMALIZATION = {
    # List types
    "list": "list",
    "array": "list",
    # Dict types
    "dict": "dict",
    "object": "dict",
    # Integer types
    "int": "int",
    "integer": "int",
    # Float types
    "float": "float",
    "number": "float",
    # String types
    "str": "str",
    "string": "str",
    # Boolean types
    "bool": "bool",
    "boolean": "bool",
    # None types
    "none": "NoneType",
    "nonetype": "NoneType",
    "null": "NoneType",
    # Datetime types (pass-through)
    "datetime": "datetime",
    "date": "date",
    "time": "time",
    "timedelta": "timedelta",
    # Special types
    "vector": "vector",
    "image": "image",
    "audio": "audio",
    "any": "Any",
}


def normalize_field_type(ft: str) -> str:
    """
    Normalize a FieldType string to a Python type name.

    Handles generic types like "List[int]", "Dict[str, Any]" by
    extracting the base type.

    Args:
        ft: The field type string from FieldType table

    Returns:
        Normalized Python type name

    Examples:
        normalize_field_type("List[int]") -> "list"
        normalize_field_type("Dict[str, int]") -> "dict"
        normalize_field_type("INTEGER") -> "int"
        normalize_field_type("boolean") -> "bool"
    """
    if not ft:
        return ft

    ft_lower = ft.lower()

    # Check for generic types first (List[...], Dict[...])
    if ft_lower.startswith("list[") or ft_lower.startswith("list<"):
        return "list"
    if ft_lower.startswith("dict[") or ft_lower.startswith("dict<"):
        return "dict"
    if ft_lower.startswith("tuple[") or ft_lower.startswith("tuple<"):
        return "tuple"
    if ft_lower.startswith("set[") or ft_lower.startswith("set<"):
        return "set"
    if ft_lower.startswith("union[") or ft_lower.startswith("union<"):
        return "union"

    # Look up in normalization map
    normalized = TYPE_NORMALIZATION.get(ft_lower)
    if normalized:
        return normalized

    # Return as-is for unknown types (preserving case for special types)
    return ft


def normalize_type(type_str: str) -> str:
    """
    Normalize type strings to their base types for consistent handling.

    This is an alias for normalize_field_type for backward compatibility.

    Args:
        type_str: The type string to normalize

    Returns:
        Normalized type string

    Examples:
        normalize_type("List[int]") -> "list"
        normalize_type("Dict[str, int]") -> "dict"
        normalize_type("DICT") -> "dict"
        normalize_type("array") -> "list"
        normalize_type("NoneType") -> "NoneType"
    """
    if not type_str:
        return type_str

    t_lower = type_str.lower()

    # List types
    if (
        t_lower == "list"
        or t_lower.startswith("list[")
        or t_lower.startswith("list<")
        or t_lower == "array"
    ):
        return "list"

    # Dict types
    if (
        t_lower == "dict"
        or t_lower.startswith("dict[")
        or t_lower.startswith("dict<")
        or t_lower == "object"
    ):
        return "dict"

    # Return as-is for other types (preserving case for special types like "NoneType")
    return type_str


def get_python_type_literal(ft: str) -> str:
    """
    Get the Python type name to return as a literal for type() operations.

    This handles the conversion from FieldType strings to the expected
    Python type name strings.

    Args:
        ft: The field type string from FieldType table

    Returns:
        Python type name string suitable for literal return
    """
    normalized = normalize_field_type(ft)

    # The normalize_field_type handles most cases, but we need to
    # ensure certain types return the right Python representation
    if normalized in ("list", "dict", "str", "int", "float", "bool"):
        return normalized
    if normalized == "NoneType":
        return "NoneType"

    # For other types, return the normalized value
    return normalized


def is_collection_type(type_str: str) -> bool:
    """
    Check if a type string represents a collection type.

    Args:
        type_str: The type string to check

    Returns:
        True if the type is a collection (list, dict, tuple, set)
    """
    normalized = normalize_field_type(type_str)
    return normalized in ("list", "dict", "tuple", "set")


def is_numeric_type(type_str: str) -> bool:
    """
    Check if a type string represents a numeric type.

    Args:
        type_str: The type string to check

    Returns:
        True if the type is numeric (int or float)
    """
    normalized = normalize_field_type(type_str)
    return normalized in ("int", "float")


def is_temporal_type(type_str: str) -> bool:
    """
    Check if a type string represents a temporal type.

    Args:
        type_str: The type string to check

    Returns:
        True if the type is temporal (datetime, date, time, timedelta)
    """
    normalized = normalize_field_type(type_str)
    return normalized in ("datetime", "date", "time", "timedelta")
