"""Utility helpers for vector embeddings.

This module centralises common vector-related operations so that the rest of the
code-base can depend on **one** place for:

1. Fetching an OpenAI (or compatible) embedding for a piece of text.
2. Basic distance / similarity calculations that are frequently used when
   working with embeddings.

The purpose of keeping all of these helpers in a single module is to make it
trivial to switch embedding providers or distance implementations later—only
this file needs to change.

NOTE: These helpers are purposely synchronous because they will usually be
called from background tasks or other I/O-bound contexts.  If you need an async
interface you can easily write a thin wrapper around ``embed`` that runs it in
an executor.
"""
from __future__ import annotations

import logging
from typing import List

from openai import OpenAI

__all__ = [
    "embed",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client instance (module-level to avoid re-initialising on every call)
# ---------------------------------------------------------------------------
_client: OpenAI | None = None


def _get_client() -> OpenAI:  # noqa: WPS430 (module-internal helper)
    """Return a (cached) instance of :class:`openai.OpenAI`."""
    global _client  # noqa: WPS420 (allow global for simple cache)

    if _client is None:
        _client = OpenAI()  # will auto-read env vars OPENAI_API_KEY, etc.
    return _client


# ---------------------------------------------------------------------------
# Embedding fetch helper
# ---------------------------------------------------------------------------


def embed(text: str, model: str = "text-embedding-3-large") -> List[float]:
    """Return the *embedding* of *text* produced by *model*.

    Parameters
    ----------
    text:
        The input string to be embedded.
    model:
        Name of the embedding model.  Defaults to the current recommended
        OpenAI model (``text-embedding-3-large``).  Override at call-site if
        you want to use a different model or provider.

    Returns
    -------
    List[float]
        The embedding vector.
    """
    client = _get_client()

    try:
        response = client.embeddings.create(input=[text], model=model)
    except Exception as exc:  # pragma: no cover – network / API failure
        _logger.exception("Failed to create embedding with model %s", model)
        raise

    return response.data[0].embedding  # type: ignore[return-value]
