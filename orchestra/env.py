"""
Environment variable utilities with ORCHESTRA_ prefix fallback support.

Orchestra uses ORCHESTRA_ prefixed environment variables for configuration,
but many variables (especially API keys) have industry-standard unprefixed names.
This module provides utilities to read ORCHESTRA_ vars with automatic fallback
to their unprefixed equivalents.

Precedence: ORCHESTRA_FOO > FOO > default

Example:
    # Returns ORCHESTRA_OPENAI_API_KEY if set, else OPENAI_API_KEY, else None
    api_key = get_env("ORCHESTRA_OPENAI_API_KEY")
"""

import os
from typing import Optional

# Mapping of ORCHESTRA_ prefixed vars to their standard unprefixed equivalents.
# Only vars with well-known standard names should be listed here.
# Internal Orchestra config (DB settings, logging, etc.) should NOT have fallbacks.
STANDARD_FALLBACKS: dict[str, str] = {
    # OpenAI API key (used for embeddings in log search)
    "ORCHESTRA_OPENAI_API_KEY": "OPENAI_API_KEY",
    # GCP/VertexAI credentials
    "ORCHESTRA_VERTEXAI_SERVICE_ACC_JSON": "GOOGLE_APPLICATION_CREDENTIALS",
    "ORCHESTRA_VERTEXAI_PROJECT": "GCP_PROJECT_ID",
    "ORCHESTRA_VERTEXAI_LOCATION": "GCP_LOCATION",
}


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Get environment variable with ORCHESTRA_ prefix fallback support.

    For keys in STANDARD_FALLBACKS, checks the prefixed version first,
    then falls back to the unprefixed standard name.

    Args:
        key: Environment variable name (typically ORCHESTRA_ prefixed)
        default: Default value if neither prefixed nor unprefixed var is set

    Returns:
        The value of the environment variable, or default if not found.

    Examples:
        >>> get_env("ORCHESTRA_OPENAI_API_KEY")
        # Returns ORCHESTRA_OPENAI_API_KEY if set, else OPENAI_API_KEY, else None

        >>> get_env("ORCHESTRA_DB_USER", "postgres")
        # Returns ORCHESTRA_DB_USER if set, else "postgres" (no fallback - internal config)
    """
    # Check the primary key first
    value = os.getenv(key)
    if value is not None:
        return value

    # Check for standard fallback if this key has one
    fallback_key = STANDARD_FALLBACKS.get(key)
    if fallback_key:
        value = os.getenv(fallback_key)
        if value is not None:
            return value

    return default


def get_env_bool(key: str, default: bool = False) -> bool:
    """
    Get boolean environment variable with ORCHESTRA_ prefix fallback support.

    Args:
        key: Environment variable name
        default: Default value if not set

    Returns:
        True if value is "true" or "1" (case-insensitive), else False.
    """
    value = get_env(key)
    if value is None:
        return default
    return value.lower() in ("true", "1")
