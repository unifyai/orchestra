"""
AST utility functions for inspecting and parsing filter_dict AST nodes.

This module provides a single source of truth for AST node inspection,
replacing scattered isinstance(dict) and dict.get("type") patterns
throughout the codebase.
"""

from typing import Any, List, Optional, Tuple

__all__ = [
    "is_identifier_node",
    "is_base_node",
    "is_image_node",
    "is_literal_node",
    "is_type_literal_node",
    "get_identifier_value",
    "get_node_type",
    "get_node_operand",
    "parse_base_params",
    "is_supported_temporal_node",
    "extract_key_from_node",
]


def is_identifier_node(node: Any) -> bool:
    """
    Check if node is an identifier AST node.

    Args:
        node: Any value that might be an AST node

    Returns:
        True if node is a dict with type="identifier"
    """
    return isinstance(node, dict) and node.get("type") == "identifier"


def is_base_node(node: Any) -> bool:
    """
    Check if node is a BASE expression.

    BASE expressions reference values from other log events.
    Format: BASE([event_ids], key)

    Args:
        node: Any value that might be an AST node

    Returns:
        True if node is a dict with operand="BASE"
    """
    return isinstance(node, dict) and node.get("operand") == "BASE"


def is_image_node(node: Any) -> bool:
    """
    Check if node is an image literal.

    Args:
        node: Any value that might be an AST node

    Returns:
        True if node is a dict with type="image"
    """
    return isinstance(node, dict) and node.get("type") == "image"


def is_literal_node(node: Any) -> bool:
    """
    Check if node is a literal value node.

    Args:
        node: Any value that might be an AST node

    Returns:
        True if node is a dict with type="literal"
    """
    return isinstance(node, dict) and node.get("type") == "literal"


def is_type_literal_node(node: Any) -> bool:
    """
    Check if node is a type literal node.

    Args:
        node: Any value that might be an AST node

    Returns:
        True if node is a dict with type="type_literal"
    """
    return isinstance(node, dict) and node.get("type") == "type_literal"


def get_identifier_value(node: Any) -> Optional[str]:
    """
    Extract value from an identifier node.

    Args:
        node: An AST node (should be an identifier node)

    Returns:
        The identifier's value string, or None if not an identifier node
    """
    if is_identifier_node(node):
        return node.get("value")
    return None


def get_node_type(node: Any) -> Optional[str]:
    """
    Get the type of an AST node.

    Args:
        node: Any value that might be an AST node

    Returns:
        The node's "type" field value, or None if not a dict or no type
    """
    if isinstance(node, dict):
        return node.get("type")
    return None


def get_node_operand(node: Any) -> Optional[str]:
    """
    Get the operand of an AST node.

    Args:
        node: Any value that might be an AST node

    Returns:
        The node's "operand" field value, or None if not a dict or no operand
    """
    if isinstance(node, dict):
        return node.get("operand")
    return None


def parse_base_params(node: Any) -> Tuple[Optional[List[int]], Optional[str]]:
    """
    Parse BASE(event_ids, key) expression parameters.

    Args:
        node: An AST node (should be a BASE expression)

    Returns:
        Tuple of (event_ids list, key string), or (None, None) if not a valid BASE node
    """
    if not is_base_node(node):
        return None, None

    rhs_args = node.get("rhs", [])
    if len(rhs_args) < 2:
        return None, None

    # Parse event_ids (first argument)
    event_ids_arg = rhs_args[0]
    if isinstance(event_ids_arg, list):
        event_ids = event_ids_arg
    else:
        event_ids = None

    # Parse key (second argument)
    key_arg = rhs_args[1]
    if isinstance(key_arg, str):
        key = key_arg
    elif is_identifier_node(key_arg):
        key = key_arg.get("value")
    else:
        key = None

    return event_ids, key


def is_supported_temporal_node(node: Any) -> bool:
    """
    Check if node can produce a naive timestamp expression.

    This function validates that the node type is supported by
    temporal_utils.build_naive_timestamp_expr for datetime operations.

    Supported node types for temporal operations:
    - identifier: Direct field reference (extracts datetime from JSONB data->>'key')
    - BASE: Reference to another log event's field (BASE([event_ids], key))
    - literal: A literal datetime string value (e.g., "2023-01-01T00:00:00Z")

    All supported nodes are processed through strip_timezone_and_cast to ensure
    consistent timezone handling for datetime arithmetic operations.

    Args:
        node: Any value that might be an AST node

    Returns:
        True if the node type is supported for temporal operations

    See Also:
        temporal_utils.build_naive_timestamp_expr: The function that builds SQL expressions for these nodes
    """
    if not isinstance(node, dict):
        return False

    node_type = node.get("type")
    if node_type in ("identifier", "literal"):
        return True

    # BASE expressions are also supported
    if node.get("operand") == "BASE":
        return True

    return False


def extract_key_from_node(node: Any) -> Optional[str]:
    """
    Extract the field key from an identifier or BASE node.

    Args:
        node: An AST node

    Returns:
        The field key string, or None if not extractable
    """
    if is_identifier_node(node):
        return node.get("value")

    if is_base_node(node):
        _, key = parse_base_params(node)
        return key

    return None
