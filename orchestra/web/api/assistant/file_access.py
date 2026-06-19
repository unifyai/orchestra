"""Workspace file-access policy semantics.

Resolves whether a Drive / SharePoint / OneDrive item is accessible to a
connected assistant account, given an explicit set of allow/deny decisions and
a ``default_allow`` fallback.

This logic is intentionally pure and dependency-free so the assistant runtime
(droid) can mirror it verbatim — the policy must resolve identically on both
the configuration side (Console -> Orchestra) and the enforcement side.
"""

from __future__ import annotations

from typing import Any, Iterable

PROVIDERS = ("google", "microsoft")

# Decision kinds.
KIND_FOLDER = "folder"
KIND_FILE = "file"


def decision_key(drive_id: str, item_id: str) -> tuple[str, str]:
    """Stable lookup key for a decision / tree node."""
    return (drive_id or "", item_id or "")


def index_decisions(decisions: Iterable[dict[str, Any]]) -> dict[tuple[str, str], bool]:
    """Build a ``{(drive_id, item_id): allow}`` lookup from raw decisions."""
    index: dict[tuple[str, str], bool] = {}
    for entry in decisions or []:
        key = decision_key(entry.get("drive_id", ""), entry.get("item_id", ""))
        index[key] = bool(entry.get("allow"))
    return index


def evaluate_access(
    decisions: Iterable[dict[str, Any]],
    default_allow: bool,
    parent_chain: list[tuple[str, str]],
) -> bool:
    """Return ``True`` if the item at the head of *parent_chain* is allowed.

    ``parent_chain`` is ordered from the item itself outward to its root
    ancestor as ``(drive_id, item_id)`` tuples.  The nearest entry carrying an
    explicit decision wins; absent any decision, ``default_allow`` applies.
    """
    index = index_decisions(decisions)
    for key in parent_chain:
        if key in index:
            return index[key]
    return bool(default_allow)
