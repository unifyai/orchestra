"""Shared context path helpers for Orchestra-owned context names."""

from __future__ import annotations

from typing import Final

SPACE_CONTEXT_PREFIX: Final[str] = "Spaces/"


def is_space_context_name(name: str | None) -> bool:
    """Return whether a context name is rooted under shared space memory."""

    return (name or "").strip("/").startswith(SPACE_CONTEXT_PREFIX)
