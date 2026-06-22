"""Provider-neutral helper utilities shared by integration adapters.

These modules hold no provider-specific wire-format knowledge. ``normalization``
encodes Orchestra's canonical conventions (behavior hints, action classes,
slugs) that every adapter maps its raw payloads onto; ``pagination`` provides
safe, bounded cursor pagination for provider catalog APIs. Concrete adapters and
``base.py`` live one level up; anything reusable across more than one backend
belongs here.
"""

from orchestra.integrations.providers.utils.normalization import (
    action_class_from_behavior_hints,
    action_class_from_hints,
    behavior_hints_from_action_class,
    confirmation_required_for_action_class,
    normalize_behavior_hints,
    normalized_behavior_hints,
    provider_tags,
    slugify,
)
from orchestra.integrations.providers.utils.pagination import (
    CursorPage,
    PaginationLimits,
    ProviderPaginationError,
    collect_cursor_pages,
    int_or_none,
)

__all__ = [
    "CursorPage",
    "PaginationLimits",
    "ProviderPaginationError",
    "action_class_from_behavior_hints",
    "action_class_from_hints",
    "behavior_hints_from_action_class",
    "collect_cursor_pages",
    "confirmation_required_for_action_class",
    "int_or_none",
    "normalize_behavior_hints",
    "normalized_behavior_hints",
    "provider_tags",
    "slugify",
]
