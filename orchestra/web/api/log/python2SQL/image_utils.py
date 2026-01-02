"""
Image utility functions for pHash computation and image processing.

This module consolidates image processing logic that was previously
inlined in jsonb_builder.py's pHash distance handler.
"""

import base64
import io
from typing import Any, Optional

__all__ = [
    "compute_phash_from_base64",
    "get_phash_from_node",
    "is_phash_hex",
]

try:
    import imagehash
    from PIL import Image

    HAS_IMAGE_LIBS = True
except ImportError:
    HAS_IMAGE_LIBS = False

from .ast_utils import is_image_node


def compute_phash_from_base64(b64_string: str) -> Optional[str]:
    """
    Compute pHash from a base64 encoded image string.

    Handles both raw base64 strings and data URI format
    (e.g., "data:image/png;base64,...").

    Args:
        b64_string: Base64 encoded image data

    Returns:
        16-character hex string of the pHash, or None if computation fails
    """
    if not HAS_IMAGE_LIBS:
        return None

    try:
        # Handle data URI format by extracting the base64 part
        if "," in b64_string:
            b64_string = b64_string.split(",")[1]

        image_data = base64.b64decode(b64_string)
        image = Image.open(io.BytesIO(image_data))
        hash_value = imagehash.phash(image)

        # Format as 16-character hex string (64-bit hash)
        return format(int(str(hash_value), 16), "016x")
    except Exception:
        return None


def get_phash_from_node(node: Any) -> Optional[str]:
    """
    Extract and compute pHash from an image literal node.

    Args:
        node: AST node that may be an image literal

    Returns:
        16-character hex pHash string, or None if not an image node
        or computation fails
    """
    if not is_image_node(node):
        return None

    value = node.get("value")
    if not value:
        return None

    return compute_phash_from_base64(value)


def is_phash_hex(value: str) -> bool:
    """
    Check if a string looks like a pHash hex value.

    A valid pHash hex string is 16 characters of hex digits.

    Args:
        value: String to check

    Returns:
        True if the string appears to be a pHash hex value
    """
    if not isinstance(value, str) or len(value) != 16:
        return False

    try:
        int(value, 16)
        return True
    except ValueError:
        return False
