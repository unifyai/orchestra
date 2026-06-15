"""Shared context path helpers for Orchestra-owned context names."""

from __future__ import annotations

from typing import Final

TEAM_CONTEXT_PREFIX: Final[str] = "Teams/"


def is_team_context_name(name: str | None) -> bool:
    """Return whether a context name is rooted under shared team memory."""

    return (name or "").strip("/").startswith(TEAM_CONTEXT_PREFIX)
