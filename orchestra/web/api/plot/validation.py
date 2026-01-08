"""Shared validation constants and functions for Plot API.

This module provides validation constants and helper functions used by both:
- Pydantic schema validators (schema.py)
- LLM inference validation (llm_inference.py)
- Runtime validation in views (views.py)
"""

import re
from typing import Any, Dict, List

# =============================================================================
# Valid Values for Plot Configuration
# =============================================================================

VALID_PLOT_TYPES = ["scatter", "bar", "histogram", "line"]
VALID_SCALES = ["linear", "log"]
VALID_AGGREGATES = ["sum", "mean", "count", "min", "max"]
VALID_METRICS = ["mean", "sum", "count", "min", "max"]
VALID_SORT_ORDER = ["unsorted", "asc", "desc"]

# Hex color pattern: #RRGGBB or #RGB
HEX_COLOR_PATTERN = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

# =============================================================================
# Plot Type Requirements
# =============================================================================

PLOT_TYPE_REQUIREMENTS: Dict[str, Dict[str, Any]] = {
    "scatter": {
        "required": ["x_axis", "y_axis"],
        "optional": [
            "group_by",
            "show_regression",
            "scale_x",
            "scale_y",
            "title",
            "x_label",
            "y_label",
        ],
        "numeric_required": ["x_axis", "y_axis"],
    },
    "bar": {
        "required": ["x_axis", "y_axis"],
        "optional": [
            "aggregate",
            "group_by",
            "metric",
            "sort_order",
            "title",
            "x_label",
            "y_label",
        ],
        "numeric_required": [],
    },
    "histogram": {
        "required": ["x_axis"],
        "optional": ["bin_count", "scale_x", "title", "x_label", "y_label"],
        "numeric_required": ["x_axis"],
    },
    "line": {
        "required": ["x_axis", "y_axis"],
        "optional": [
            "group_by",
            "scale_x",
            "scale_y",
            "title",
            "x_label",
            "y_label",
        ],
        "numeric_required": ["y_axis"],
    },
}


# =============================================================================
# Validation Functions
# =============================================================================


def validate_hex_color(color: str) -> bool:
    """
    Validate a hex color string.

    Args:
        color: Color string to validate (e.g., "#FF0000" or "#F00")

    Returns:
        True if valid hex color, False otherwise
    """
    return bool(HEX_COLOR_PATTERN.match(color))


def validate_colors_dict(colors: Dict[str, str]) -> List[str]:
    """
    Validate a dictionary of colors.

    Args:
        colors: Dictionary mapping group values to hex colors

    Returns:
        List of invalid color entries (empty if all valid)
    """
    invalid = []
    for key, color in colors.items():
        if not validate_hex_color(color):
            invalid.append(f"{key}: {color}")
    return invalid


def validate_field_exists(field_name: str, available_fields: List[str]) -> bool:
    """
    Check if a field exists in the available fields.

    Args:
        field_name: Name of the field to check
        available_fields: List of available field names

    Returns:
        True if field exists, False otherwise
    """
    return field_name in available_fields


def get_plot_type_requirements(plot_type: str) -> Dict[str, Any]:
    """
    Get the requirements for a specific plot type.

    Args:
        plot_type: The plot type (scatter, bar, histogram, line)

    Returns:
        Dictionary with 'required', 'optional', and 'numeric_required' keys

    Raises:
        ValueError: If plot_type is not valid
    """
    if plot_type not in VALID_PLOT_TYPES:
        raise ValueError(
            f"Invalid plot type '{plot_type}'. "
            f"Must be one of: {', '.join(VALID_PLOT_TYPES)}",
        )
    return PLOT_TYPE_REQUIREMENTS[plot_type]
