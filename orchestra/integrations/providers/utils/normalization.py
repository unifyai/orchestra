"""Provider-neutral normalization helpers shared by integration adapters.

These helpers contain no provider-specific wire-format knowledge. They encode
Orchestra's canonical conventions (behavior hints, action classes, slugs) that
every provider adapter maps its raw payloads onto. Provider-specific parsing
lives in the individual adapter modules; anything reusable across more than one
backend belongs here so the generic sync/API layers never branch per provider.
"""

from __future__ import annotations

from typing import Any

ORCHESTRA_BEHAVIOR_HINT_ORDER = (
    "read_only",
    "mutates_state",
    "destructive",
    "sensitive_data",
    "bulk_data",
    "idempotent",
    "external",
    "creates_resource",
    "updates_resource",
    "unknown_effects",
)

CONFIRMATION_ACTION_CLASSES = {"write", "destructive", "bulk_export"}


def slugify(value: str) -> str:
    normalized = "".join(
        char.lower() if char.isalnum() else "_" for char in value.strip()
    )
    return "_".join(part for part in normalized.split("_") if part)


def provider_tags(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(tag) for tag in value if tag}
    if isinstance(value, dict):
        return {str(tag) for tag, enabled in value.items() if enabled}
    return set()


def normalized_behavior_hints(
    *,
    tags: set[str],
    annotations: dict[str, Any] | None = None,
) -> list[str]:
    annotations = annotations or {}
    destructive = (
        "destructiveHint" in tags or annotations.get("destructiveHint") is True
    )
    creates = "createHint" in tags or annotations.get("createHint") is True
    updates = "updateHint" in tags or annotations.get("updateHint") is True
    read_only = "readOnlyHint" in tags or annotations.get("readOnlyHint") is True
    mutates = (
        destructive or creates or updates or annotations.get("readOnlyHint") is False
    )

    hints: set[str] = set()
    if read_only and not mutates:
        hints.add("read_only")
    if mutates:
        hints.add("mutates_state")
    if destructive:
        hints.add("destructive")
    if "idempotentHint" in tags or annotations.get("idempotentHint") is True:
        hints.add("idempotent")
    if "openWorldHint" in tags or annotations.get("openWorldHint") is True:
        hints.add("external")
    if creates:
        hints.add("creates_resource")
    if updates:
        hints.add("updates_resource")
    if not hints:
        hints.add("unknown_effects")
    return [hint for hint in ORCHESTRA_BEHAVIOR_HINT_ORDER if hint in hints]


def normalize_behavior_hints(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    allowed = set(ORCHESTRA_BEHAVIOR_HINT_ORDER)
    normalized = [str(hint) for hint in value if str(hint) in allowed]
    seen: set[str] = set()
    return [hint for hint in normalized if not (hint in seen or seen.add(hint))]


def action_class_from_behavior_hints(behavior_hints: list[str]) -> str:
    hints = set(behavior_hints)
    if "destructive" in hints:
        return "destructive"
    if "bulk_data" in hints:
        return "bulk_export"
    if "sensitive_data" in hints and "read_only" in hints:
        return "sensitive_read"
    if (
        "mutates_state" in hints
        or "creates_resource" in hints
        or "updates_resource" in hints
    ):
        return "write"
    if "read_only" in hints:
        return "read"
    return "write"


def behavior_hints_from_action_class(action_class: str | None) -> list[str]:
    if action_class == "read":
        return ["read_only"]
    if action_class == "sensitive_read":
        return ["read_only", "sensitive_data"]
    if action_class == "bulk_export":
        return ["read_only", "bulk_data"]
    if action_class == "destructive":
        return ["mutates_state", "destructive"]
    if action_class == "write":
        return ["mutates_state"]
    return ["unknown_effects"]


def action_class_from_hints(
    *,
    tags: set[str],
    annotations: dict[str, Any] | None = None,
) -> str:
    return action_class_from_behavior_hints(
        normalized_behavior_hints(tags=tags, annotations=annotations),
    )


def confirmation_required_for_action_class(action_class: str | None) -> bool:
    return action_class in CONFIRMATION_ACTION_CLASSES
