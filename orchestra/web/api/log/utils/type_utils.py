"""Utilities for parsing and handling explicit types in log fields."""

import re
from typing import List, Optional, Tuple

# Supported base types that map to SQL types
# These are the fundamental types that can be stored in the database
SUPPORTED_BASE_TYPES = [
    "bool",
    "int",
    "float",
    "str",
    "datetime",
    "time",
    "date",
    "timedelta",
    "dict",
    "list",
    "image",  # Base64-encoded images (detected via magic bytes)
    "audio",  # Base64-encoded audio (detected via magic bytes)
]

# Special types that don't map to SQL but are valid field types
SPECIAL_FIELD_TYPES = [
    "Any",  # Untyped/mixed-type fields
    "NoneType",  # Weak None type (allowed with any strong type)
    "enum",  # Enum type with restricted values (Log.inferred_type will be "str")
]

# Default field type for untyped/mixed-type fields
# This is used when no type is specified during field creation
# "Any" means the field is NOT strongly typed and can accept logs of any/mixed types
DEFAULT_FIELD_TYPE = "Any"


def normalize_type_string(type_str: str) -> str:
    """
    Normalize a type string to a canonical format.

    Examples:
        "int" -> "int"
        "Int" -> "int"
        "ANY" -> "Any"
        "nonetype" -> "NoneType"
        "ENUM" -> "enum"
        "LIST[INT]" -> "List[int]"
        "Dict[Str, Float]" -> "Dict[str, float]"
        "list[image]" -> "List[image]"

    Args:
        type_str: The type string to normalize

    Returns:
        Normalized type string with proper casing
    """
    if not type_str:
        return type_str

    # Handle simple types (no brackets)
    if "[" not in type_str:
        # Special types with specific casing
        lower_type = type_str.lower()
        if lower_type == "any":
            return "Any"
        elif lower_type == "nonetype":
            return "NoneType"
        elif lower_type == "enum":
            return "enum"
        else:
            return type_str.lower()

    # Parse nested types using regex
    # Match pattern: Type[InnerType] or Type[Key, Value]
    match = re.match(r"^(\w+)\[(.*)\]$", type_str.strip())
    if not match:
        # If it doesn't match the expected pattern, just lowercase it
        return type_str.lower()

    outer_type = match.group(1)
    inner_types = match.group(2)

    # Normalize outer type: List, Dict, Set, Tuple get capitalized
    collection_types = {"list", "dict", "set", "tuple"}
    if outer_type.lower() in collection_types:
        outer_type = outer_type.capitalize()
    else:
        outer_type = outer_type.lower()

    # Normalize inner types (split by comma and normalize each)
    if "," in inner_types:
        # Dict-like types with key-value
        parts = [part.strip() for part in inner_types.split(",")]
        normalized_parts = [normalize_type_string(part) for part in parts]
        inner_str = ", ".join(normalized_parts)
    else:
        # Simple nested type
        inner_str = normalize_type_string(inner_types.strip())

    return f"{outer_type}[{inner_str}]"


def parse_nested_type(type_str: str) -> Tuple[str, Optional[List[str]]]:
    """
    Parse a nested type string into base type and inner types.

    Examples:
        "int" -> ("int", None)
        "List[int]" -> ("List", ["int"])
        "Dict[str, float]" -> ("Dict", ["str", "float"])

    Args:
        type_str: The type string to parse (should be normalized)

    Returns:
        Tuple of (base_type, inner_types)
        inner_types is None for simple types, or a list for nested types
    """
    if not type_str or "[" not in type_str:
        return (type_str, None)

    match = re.match(r"^(\w+)\[(.*)\]$", type_str.strip())
    if not match:
        return (type_str, None)

    base_type = match.group(1)
    inner_str = match.group(2)

    # Split by comma for Dict-like types
    if "," in inner_str:
        inner_types = [part.strip() for part in inner_str.split(",")]
    else:
        inner_types = [inner_str.strip()]

    return (base_type, inner_types)


def is_image_type(type_str: str) -> bool:
    """
    Check if a type string represents an image type.

    Examples:
        "image" -> True
        "Image" -> True
        "List[image]" -> True
        "str" -> False

    Args:
        type_str: The type string to check

    Returns:
        True if the type represents images
    """
    normalized = normalize_type_string(type_str)

    # Check if it's directly an image type
    if normalized == "image":
        return True

    # Check if it's a collection of images
    base_type, inner_types = parse_nested_type(normalized)
    if inner_types:
        return any("image" in inner.lower() for inner in inner_types)

    return False


def get_base_storage_type(type_str: str) -> str:
    """
    Get the base storage type for a given type string.
    This is the type that will be stored in the database's inferred_type field.

    For simple types, returns the type as-is.
    For nested types, returns the outer collection type.

    Examples:
        "int" -> "int"
        "List[int]" -> "list"
        "Dict[str, float]" -> "dict"
        "image" -> "image"
        "List[image]" -> "list"

    Args:
        type_str: The type string

    Returns:
        The base storage type
    """
    normalized = normalize_type_string(type_str)
    base_type, inner_types = parse_nested_type(normalized)

    if inner_types is None:
        # Simple type
        return normalized
    else:
        # Nested type - return the outer type in lowercase
        return base_type.lower()


def is_untyped_field(field_type: str) -> bool:
    """
    Check if a field type represents an untyped/mixed-type field.

    Args:
        field_type: The field type string to check

    Returns:
        True if the field is untyped (accepts any/mixed types), False otherwise
    """
    return (
        field_type == DEFAULT_FIELD_TYPE
        or field_type.lower() == DEFAULT_FIELD_TYPE.lower()
    )


def get_display_type(type_str: str, stored_type: Optional[str] = None) -> str:
    """
    Get the display type for the get_fields endpoint.

    If an explicit type was provided, return it (normalized).
    Otherwise, return the stored/inferred type.

    Args:
        type_str: The explicit type string (may be None)
        stored_type: The stored inferred type

    Returns:
        The type to display to users
    """
    if type_str:
        return normalize_type_string(type_str)
    return stored_type or DEFAULT_FIELD_TYPE


def is_valid_field_type(type_str: str) -> bool:
    """
    Check if a type string is a valid field type.

    Valid types include:
    - Special types: "Any", "NoneType", "enum"
    - Base types: "int", "str", "float", etc.
    - Nested types: "List[int]", "Dict[str, float]", etc.

    Args:
        type_str: The type string to check (should be normalized)

    Returns:
        True if valid, False otherwise
    """
    normalized = normalize_type_string(type_str)

    # Check special types
    if normalized in SPECIAL_FIELD_TYPES:
        return True

    # Check base types
    if normalized.lower() in SUPPORTED_BASE_TYPES:
        return True

    # Check nested types
    base_type, inner_types = parse_nested_type(normalized)
    if inner_types:
        # It's a nested type - validate the base
        return base_type.lower() in SUPPORTED_BASE_TYPES

    return False


def types_match(field_type: str, inferred_type: str) -> bool:
    """
    Check if an inferred type matches a field type.

    This handles nested types and special cases:
    - "List[int]" matches "list" (base type match)
    - "List[int]" matches "List[int]" (exact match)
    - "Dict[str, float]" matches "dict" (base type match)
    - "int" matches "int" (exact match)
    - "enum" matches "str" (enum values are always strings)
    - "NoneType" is a weak type and matches ANY field type (including strict types)

    Args:
        field_type: The field's declared type (normalized)
        inferred_type: The inferred type from a value (normalized)

    Returns:
        True if types match, False otherwise
    """
    # Normalize both types
    norm_field = normalize_type_string(field_type)
    norm_inferred = normalize_type_string(inferred_type)

    # Exact match
    if norm_field == norm_inferred:
        return True

    # Case-insensitive match
    if norm_field.lower() == norm_inferred.lower():
        return True

    # Special case: enum field type always stores string values
    # So FieldType.field_type="enum" should match Log.inferred_type="str"
    if norm_field.lower() == "enum" and norm_inferred.lower() == "str":
        return True

    # Weak type: NoneType is allowed for any field type (including strict types)
    if norm_inferred == "NoneType" or norm_field == "NoneType":
        return True

    # Check if field type is nested and inferred type matches the base
    # E.g., field_type="List[int]", inferred_type="list"
    field_base, field_inner = parse_nested_type(norm_field)
    if field_inner:
        # Field is nested - check if inferred matches the base type
        if field_base.lower() == norm_inferred.lower():
            return True

    return False
