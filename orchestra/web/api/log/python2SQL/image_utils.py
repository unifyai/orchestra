"""
Image utility functions for pHash computation and image processing.

This module consolidates image processing logic that was previously
inlined in jsonb_builder.py's pHash distance handler.
"""

import base64
import io
import logging
import time
from typing import Any, Optional

__all__ = [
    "compute_phash_from_base64",
    "fetch_media_with_retry",
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


def fetch_media_with_retry(
    bucket_service,
    filename: str,
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> Optional[str]:
    """
    Fetch media from GCS with exponential backoff retry.

    Handles GCS eventual consistency by retrying failed fetches with
    exponential backoff. This ensures newly uploaded objects are
    retrievable even if they haven't fully propagated yet.

    Args:
        bucket_service: BucketService instance for fetching media
        filename: The filename to fetch from GCS
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds between retries (default: 0.5)

    Returns:
        Base64 encoded media string, or None if all retries fail
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = bucket_service.get_media(filename)
            if result is not None:
                return result

            # get_media returned None - object not found, might be eventual consistency
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logging.debug(
                    f"GCS object '{filename}' not found, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})",
                )
                time.sleep(delay)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logging.debug(
                    f"Error fetching '{filename}' from GCS: {e}, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})",
                )
                time.sleep(delay)

    if last_error:
        logging.warning(
            f"Failed to fetch '{filename}' from GCS after {max_retries + 1} attempts: {last_error}",
        )
    else:
        logging.warning(
            f"GCS object '{filename}' not found after {max_retries + 1} attempts",
        )

    return None
