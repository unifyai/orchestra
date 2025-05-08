import logging
import os
from typing import List, Optional

__all__ = ["embed"]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Initialize OpenAI client handling both <1.0 (legacy) and >=1.0 SDK interfaces.
# --------------------------------------------------------------------------------------

try:
    # New SDK (>=1.0)
    from openai import OpenAI as _OpenAIClient  # type: ignore

    _client = _OpenAIClient(
        api_key=os.getenv("ORCHESTRA_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"),
    )
    _is_v1 = True
except ImportError:  # pragma: no cover
    # Fallback to pre-1.0 SDK
    import openai as _openai

    # If api_key not already set, pull from env
    if not _openai.api_key:
        _openai.api_key = os.getenv("ORCHESTRA_OPENAI_API_KEY") or os.getenv(
            "OPENAI_API_KEY",
        )

    _client = _openai  # type: ignore
    _is_v1 = False


def _embed_v1(text: str, model: str) -> List[float]:
    """Embedding using >=1.0 OpenAI SDK client."""
    resp = _client.embeddings.create(input=[text], model=model)
    return resp.data[0].embedding  # type: ignore[attr-defined]


def _embed_legacy(text: str, model: str) -> List[float]:
    """Embedding using <1.0 legacy OpenAI SDK."""
    resp = _client.Embedding.create(input=text, model=model)  # type: ignore[attr-defined]
    # Legacy response is dict-like
    if isinstance(resp, dict):
        return resp["data"][0]["embedding"]
    return resp.data[0].embedding  # type: ignore[attr-defined]


def embed(x: str, model: Optional[str] = "text-embedding-3-large") -> List[float]:
    """Return text embeddings via OpenAI, compatible with both SDK versions.

    Parameters
    ----------
    x: str
        Text to embed.
    model: str, optional
        Embedding model name (defaults to "text-embedding-3-large").
    """
    try:
        if _is_v1 and hasattr(_client, "embeddings"):
            return _embed_v1(x, model)
        return _embed_legacy(x, model)
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to generate embedding: %s", exc)
        raise
