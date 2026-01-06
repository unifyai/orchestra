"""LLM-based plot configuration inference.

Uses Orchestra's chat completions endpoint to infer plot configurations
from natural language descriptions.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# Valid values for plot configuration
VALID_PLOT_TYPES = ["scatter", "bar", "histogram", "line"]
VALID_SCALES = ["linear", "log"]
VALID_AGGREGATES = ["sum", "mean", "count", "min", "max"]
VALID_METRICS = ["mean", "sum", "count", "min", "max"]
VALID_SORT_BY = ["x", "y", "value", "name", "count"]
VALID_SORT_ORDER = ["asc", "desc"]

# Requirements per plot type
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
            "sort_by",
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


SYSTEM_PROMPT = """You are a data visualization expert. Given a user's description and available data fields, determine the best plot configuration.

Available plot types:
- scatter: Requires x_axis (numeric) and y_axis (numeric). Good for correlations, relationships between two variables. Can show regression lines.
- bar: Requires x_axis (categorical/string) and y_axis (numeric). Good for comparisons across categories. Supports aggregation and sorting.
- histogram: Requires x_axis (numeric) only. Good for showing distributions of a single variable. Configurable bin count.
- line: Requires x_axis (numeric/datetime) and y_axis (numeric). Good for trends over time or ordered data.

Guidelines for field selection:
- For x_axis: Prefer time/date fields for trends, categorical fields for comparisons
- For y_axis: Prefer numeric fields like counts, values, scores, latencies
- For group_by: Use categorical fields to compare multiple series (e.g., model, status, category)
- Use log scale when data spans multiple orders of magnitude
- Suggest a title and axis labels based on the user's intent

Respond with ONLY valid JSON (no markdown, no code blocks):
{
  "type": "scatter|bar|histogram|line",
  "x_axis": "field_name",
  "y_axis": "field_name or null if histogram",
  "group_by": "field_name for multi-series, or null",
  "aggregate": "sum|mean|count|min|max or null (for bar charts)",
  "scale_x": "linear|log (use log for wide-ranging numeric data)",
  "scale_y": "linear|log (use log for wide-ranging numeric data)",
  "metric": "mean|sum|count|min|max or null",
  "show_regression": true/false (scatter only, true if user wants trends/correlation),
  "bin_count": 10-50 or null (histogram only, more bins for larger datasets)",
  "sort_by": "x|y|value|name|count or null (for bar charts)",
  "sort_order": "asc|desc or null",
  "title": "Suggested descriptive title for the plot",
  "x_label": "Label for x-axis based on the field and context",
  "y_label": "Label for y-axis based on the field and context",
  "confidence": 0.0-1.0 (how confident you are in this configuration),
  "reasoning": "Brief explanation of why this configuration was chosen"
}"""


class PlotConfigInferenceError(Exception):
    """Error during plot configuration inference."""


class PlotConfigValidationError(Exception):
    """Error during plot configuration validation."""

    def __init__(self, message: str, field: str, recoverable: bool = False):
        super().__init__(message)
        self.field = field
        self.recoverable = recoverable


def _build_user_prompt(
    description: str,
    available_fields: List[str],
    field_types: Dict[str, str],
    sample_values: Optional[Dict[str, List[Any]]] = None,
) -> str:
    """Build the user prompt with field information and optional sample values."""
    field_lines = []
    for field in available_fields:
        field_type = field_types.get(field, "unknown")
        line = f"- {field}: {field_type}"
        # Add sample values if available (helps LLM understand categorical vs numeric)
        if sample_values and field in sample_values:
            samples = sample_values[field][:5]  # Limit to 5 samples
            sample_str = ", ".join(str(s) for s in samples)
            line += f" (samples: {sample_str})"
        field_lines.append(line)

    field_list = "\n".join(field_lines)

    return f"""Available fields with types:
{field_list}

User description: "{description}"

Analyze the user's intent and infer the best plot configuration. Consider:
1. Which plot type best visualizes the relationship the user wants to see
2. Which fields should be on each axis based on their types and the user's intent
3. Whether grouping would add value (for comparing categories)
4. Whether log scales are appropriate for the data ranges
5. A descriptive title and axis labels that capture the visualization's purpose
"""


def _parse_llm_response(content: str) -> Dict[str, Any]:
    """Parse LLM response, handling potential markdown code blocks."""
    json_content = content.strip()

    # Remove markdown code block if present
    if json_content.startswith("```"):
        # Remove opening fence
        lines = json_content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        json_content = "\n".join(lines)

    try:
        return json.loads(json_content)
    except json.JSONDecodeError as e:
        raise PlotConfigInferenceError(
            f"Failed to parse LLM response as JSON: {content}",
        ) from e


def _find_fallback_field(
    field_role: str,
    plot_type: str,
    available_fields: List[str],
    field_types: Optional[Dict[str, str]] = None,
    must_be_numeric: bool = False,
) -> Optional[str]:
    """Find a suitable fallback field based on plot type and requirements."""
    candidates = available_fields

    if must_be_numeric and field_types:
        numeric_types = ["int", "float", "number", "integer", "double", "decimal"]
        candidates = [
            f
            for f in available_fields
            if any(t in (field_types.get(f, "")).lower() for t in numeric_types)
        ]

    if not candidates:
        return None

    # Heuristics for common field names
    preferred_patterns: Dict[str, List[str]] = {
        "x_axis": ["time", "date", "timestamp", "created", "id", "index"],
        "y_axis": [
            "value",
            "count",
            "total",
            "amount",
            "score",
            "latency",
            "duration",
        ],
    }

    patterns = preferred_patterns.get(field_role, [])

    # Try to find a field matching patterns
    for pattern in patterns:
        match = next(
            (f for f in candidates if pattern in f.lower()),
            None,
        )
        if match:
            return match

    # If no pattern match, return first candidate
    return candidates[0] if candidates else None


def validate_plot_config(
    config: Dict[str, Any],
    available_fields: List[str],
    field_types: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Validate an LLM-inferred plot config with fallbacks where possible.

    Args:
        config: The inferred plot configuration from LLM.
        available_fields: List of available field names in the data.
        field_types: Optional map of field names to their types.

    Returns:
        Validated and possibly corrected configuration.

    Raises:
        PlotConfigValidationError: For unrecoverable validation issues.
    """
    warnings: List[str] = []
    validated = dict(config)

    # 1. Validate plot type
    plot_type = config.get("type")
    if not plot_type or plot_type not in VALID_PLOT_TYPES:
        raise PlotConfigValidationError(
            f"Invalid or missing plot type: {plot_type}. "
            f"Must be one of: {', '.join(VALID_PLOT_TYPES)}",
            "type",
            False,
        )

    # 2. Get requirements for this plot type
    requirements = PLOT_TYPE_REQUIREMENTS[plot_type]

    # 3. Check required fields exist
    for field in requirements["required"]:
        value = config.get(field)
        numeric_required = field in requirements.get("numeric_required", [])

        if not value:
            # Try to find a suitable fallback
            fallback = _find_fallback_field(
                field,
                plot_type,
                available_fields,
                field_types,
                numeric_required,
            )

            if fallback:
                validated[field] = fallback
                warnings.append(f"Missing {field}, using fallback: {fallback}")
            else:
                raise PlotConfigValidationError(
                    f"Required field '{field}' is missing for {plot_type} plot "
                    "and no suitable fallback found",
                    field,
                    False,
                )
        elif value not in available_fields:
            # Field specified but doesn't exist in data
            fallback = _find_fallback_field(
                field,
                plot_type,
                available_fields,
                field_types,
                numeric_required,
            )

            if fallback:
                validated[field] = fallback
                warnings.append(
                    f"Field '{value}' not found for {field}, using fallback: {fallback}",
                )
            else:
                raise PlotConfigValidationError(
                    f"Field '{value}' specified for {field} does not exist "
                    "in available fields",
                    field,
                    False,
                )

    # 4. Validate optional fields if provided
    group_by = config.get("group_by")
    if group_by and group_by not in available_fields:
        validated["group_by"] = None
        warnings.append(f"group_by field '{group_by}' not found, ignoring")

    # 5. Validate/default scales
    if config.get("scale_x") and config["scale_x"] not in VALID_SCALES:
        validated["scale_x"] = "linear"
        warnings.append(f"Invalid scale_x '{config['scale_x']}', defaulting to linear")

    if config.get("scale_y") and config["scale_y"] not in VALID_SCALES:
        validated["scale_y"] = "linear"
        warnings.append(f"Invalid scale_y '{config['scale_y']}', defaulting to linear")

    # 6. Validate aggregate
    if config.get("aggregate") and config["aggregate"] not in VALID_AGGREGATES:
        validated["aggregate"] = "mean"
        warnings.append(
            f"Invalid aggregate '{config['aggregate']}', defaulting to mean",
        )

    # 7. Validate metric
    if config.get("metric") and config["metric"] not in VALID_METRICS:
        validated["metric"] = "mean"
        warnings.append(f"Invalid metric '{config['metric']}', defaulting to mean")

    # 8. Validate bin_count
    if plot_type == "histogram":
        bin_count = config.get("bin_count")
        if bin_count is None:
            validated["bin_count"] = 10
        elif not isinstance(bin_count, int) or bin_count < 1 or bin_count > 100:
            validated["bin_count"] = max(
                1,
                min(100, int(bin_count) if bin_count else 10),
            )
            warnings.append(
                f"bin_count clamped to valid range: {validated['bin_count']}",
            )

    # 9. Validate sort_by and sort_order (bar charts)
    if plot_type == "bar":
        if config.get("sort_by") and config["sort_by"] not in VALID_SORT_BY:
            validated["sort_by"] = None
            warnings.append(
                f"Invalid sort_by '{config['sort_by']}', ignoring",
            )
        if config.get("sort_order") and config["sort_order"] not in VALID_SORT_ORDER:
            validated["sort_order"] = "desc"
            warnings.append(
                f"Invalid sort_order '{config['sort_order']}', defaulting to desc",
            )

    # 10. Validate title and labels (sanitize strings)
    for label_field in ["title", "x_label", "y_label"]:
        if config.get(label_field):
            # Ensure it's a string and limit length
            label_value = str(config[label_field])[:100]
            validated[label_field] = label_value

    # 11. Validate show_regression (scatter only)
    if plot_type == "scatter":
        show_regression = config.get("show_regression")
        if show_regression is not None:
            validated["show_regression"] = bool(show_regression)
    else:
        # Remove show_regression for non-scatter plots
        validated.pop("show_regression", None)

    # 12. Ensure confidence is valid
    confidence = config.get("confidence")
    if confidence is None or confidence < 0 or confidence > 1:
        validated["confidence"] = 0.5

    # Add warnings to reasoning if any
    if warnings:
        reasoning = validated.get("reasoning", "")
        validated["reasoning"] = " | ".join(
            filter(None, [reasoning, f"Validation notes: {'; '.join(warnings)}"]),
        )

    return validated


async def infer_plot_config(
    description: str,
    available_fields: List[str],
    field_types: Dict[str, str],
    api_key: str,
    orchestra_url: str = "http://localhost:8000",
    sample_values: Optional[Dict[str, List[Any]]] = None,
) -> Dict[str, Any]:
    """
    Infer plot configuration from a natural language description.

    Uses Orchestra's chat completions endpoint for LLM inference.
    The LLM call is billed to the caller's account.

    Args:
        description: Natural language description of the desired plot.
        available_fields: List of available field names in the data.
        field_types: Map of field names to their types.
        api_key: User's API key for billing.
        orchestra_url: Base URL for Orchestra API.
        sample_values: Optional sample values for each field to help LLM understand data.

    Returns:
        Validated plot configuration dictionary.

    Raises:
        PlotConfigInferenceError: If inference fails.
        PlotConfigValidationError: If the inferred config is invalid.
    """
    user_prompt = _build_user_prompt(
        description,
        available_fields,
        field_types,
        sample_values,
    )

    payload = {
        "model": "gpt-4o-mini@openai",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 500,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{orchestra_url}/v0/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code != 200:
                error_detail = response.json().get("detail", response.text)
                raise PlotConfigInferenceError(
                    f"LLM request failed with status {response.status_code}: {error_detail}",
                )

            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content")

            if not content:
                raise PlotConfigInferenceError("No response content from LLM")

            # Parse the JSON response
            parsed = _parse_llm_response(content)

            # Validate and apply fallbacks
            validated = validate_plot_config(parsed, available_fields, field_types)

            logger.info(
                f"Inferred plot config: type={validated.get('type')}, "
                f"confidence={validated.get('confidence')}",
            )

            return validated

    except httpx.RequestError as e:
        raise PlotConfigInferenceError(f"HTTP request failed: {e}") from e
    except PlotConfigValidationError:
        raise
    except Exception as e:
        if isinstance(e, PlotConfigInferenceError):
            raise
        raise PlotConfigInferenceError(f"Unexpected error during inference: {e}") from e
