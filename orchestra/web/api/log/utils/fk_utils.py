"""Utility functions for foreign key constraint handling."""

from typing import Any, Dict, List


def format_fk_violation_error(violations: List[Dict[str, Any]]) -> str:
    """Format foreign key violation details into a user-friendly error message.

    Args:
        violations: List of violation dictionaries from check_restrict_constraints

    Returns:
        Formatted error message string
    """
    if not violations:
        return "Foreign key constraint violation"

    messages = []
    for v in violations:
        action = "delete" if "delete" in v.get("fk_action", "").lower() else "update"
        msg = (
            f"Cannot {action} {v['context']}.{v['column']}={v['value']}: "
            f"{v['count']} row(s) in {v['referencing_context']}.{v['fk_column']} "
            f"reference this value ({v['fk_action']} constraint)"
        )
        messages.append(msg)

    if len(messages) == 1:
        return messages[0]

    # Multiple violations - format as numbered list
    formatted = "Multiple foreign key constraint violations:\n"
    for i, msg in enumerate(messages, 1):
        formatted += f"{i}. {msg}\n"
    return formatted.rstrip()
