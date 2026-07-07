"""Curated catalog of per-assistant default LLM options.

Each option pairs a unillm ``model@provider`` endpoint with a reasoning-effort
level. Only multimodal models (native image input) are eligible, because the
assistant runtime routes screenshots and other image content through its
default model's call sites.

An option with ``reasoning_effort=None`` leaves the runtime's per-call-site
effort levels untouched; a concrete effort overrides them wherever the default
model is used.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

PLATFORM_DEFAULT_MODEL = "minimax-v3@minimax"


@dataclass(frozen=True)
class DefaultModelOption:
    model: str
    reasoning_effort: Optional[str]
    label: str


DEFAULT_MODEL_OPTIONS: Tuple[DefaultModelOption, ...] = (
    DefaultModelOption(
        model=PLATFORM_DEFAULT_MODEL,
        reasoning_effort=None,
        label="MiniMax-M3 (platform default)",
    ),
    DefaultModelOption(
        model="gpt-5.5@openai",
        reasoning_effort="low",
        label="GPT-5.5 (low thinking)",
    ),
    DefaultModelOption(
        model="gpt-5.5@openai",
        reasoning_effort="medium",
        label="GPT-5.5 (medium thinking)",
    ),
    DefaultModelOption(
        model="gpt-5.5@openai",
        reasoning_effort="high",
        label="GPT-5.5 (high thinking)",
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="low",
        label="Claude Opus 4.8 (low thinking)",
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="medium",
        label="Claude Opus 4.8 (medium thinking)",
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="high",
        label="Claude Opus 4.8 (high thinking)",
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="low",
        label="Claude Fable 5 (low thinking)",
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="medium",
        label="Claude Fable 5 (medium thinking)",
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="high",
        label="Claude Fable 5 (high thinking)",
    ),
)

_VALID_PAIRS = {
    (option.model, option.reasoning_effort) for option in DEFAULT_MODEL_OPTIONS
}


def is_valid_default_model(
    model: str,
    reasoning_effort: Optional[str],
) -> bool:
    """Return whether (model, reasoning_effort) is a catalog option."""

    return (model, reasoning_effort) in _VALID_PAIRS
