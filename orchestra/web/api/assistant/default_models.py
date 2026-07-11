"""Curated catalog of per-assistant LLM options (actor default + slow brain).

Each option pairs a unillm ``model@provider`` endpoint with a reasoning-effort
level. Only multimodal models (native image input) are eligible, because the
assistant runtime routes screenshots and other image content through these
call sites.

An option with ``reasoning_effort=None`` leaves the runtime's per-call-site
effort levels untouched; a concrete effort overrides them wherever that model
is used.

Credit estimates (display-only; 1 USD billed provider cost = 400 credits, at a
1.2x margin on provider rates):

- ``approx_credits_per_task`` — order-of-magnitude cost of one typical
  CodeActActor / tool-loop task. High-effort figures are anchored to Artificial
  Analysis's "Cost per Intelligence Index Task"; medium/low scale to ~70%/50%
  of the high anchor. Real tasks vary widely.
- ``approx_credits_per_message`` — cost of one typical ConversationManager
  slow-brain turn, derived from raw token rates for a controlled budget of
  ~12k input tokens plus effort-scaled output (low ~400 / medium ~800 /
  high ~1500 / unset ~600). Slow-brain turns are far more constrained than
  open-ended actor tasks, so token math is a better signal than AA task
  anchors here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Optional, Tuple

PLATFORM_DEFAULT_MODEL = "gpt-5.6-sol@openai"
PLATFORM_DEFAULT_REASONING_EFFORT = "high"
PLATFORM_DEFAULT_DISPLAY_NAME = "GPT-5.6 Sol"

# Matches Unify's UNITY_CONVERSATION_SLOW_BRAIN_* defaults.
PLATFORM_SLOW_BRAIN_MODEL = "gpt-5.6-terra@openai"
PLATFORM_SLOW_BRAIN_DISPLAY_NAME = "GPT-5.6 Terra"
PLATFORM_SLOW_BRAIN_REASONING_EFFORT = "high"

_AA_MODELS_BASE_URL = "https://artificialanalysis.ai/models"

# Typical slow-brain conversational turn token budget.
_MSG_INPUT_TOKENS = 12_000
_MSG_OUTPUT_BY_EFFORT = {
    None: 600,
    "low": 400,
    "medium": 800,
    "high": 1_500,
}
_MARGIN = 1.2
_CREDITS_PER_USD = 400


@dataclass(frozen=True)
class DefaultModelOption:
    model: Optional[str]
    reasoning_effort: Optional[str]
    label: str
    approx_credits_per_task: int
    approx_credits_per_message: int
    artificial_analysis_url: str


def _aa_url(slug: str) -> str:
    return f"{_AA_MODELS_BASE_URL}/{slug}"


def _msg_credits(
    input_usd_per_m: float,
    output_usd_per_m: float,
    effort: Optional[str],
) -> int:
    """Credits for one typical slow-brain message at the given token rates."""

    out_tokens = _MSG_OUTPUT_BY_EFFORT[effort]
    usd = (
        input_usd_per_m * _MSG_INPUT_TOKENS + output_usd_per_m * out_tokens
    ) / 1_000_000
    return max(1, round(usd * _MARGIN * _CREDITS_PER_USD))


def _opt(
    *,
    model: Optional[str],
    reasoning_effort: Optional[str],
    label: str,
    approx_credits_per_task: int,
    input_usd_per_m: float,
    output_usd_per_m: float,
    aa_slug: str,
) -> DefaultModelOption:
    return DefaultModelOption(
        model=model,
        reasoning_effort=reasoning_effort,
        label=label,
        approx_credits_per_task=approx_credits_per_task,
        approx_credits_per_message=_msg_credits(
            input_usd_per_m,
            output_usd_per_m,
            reasoning_effort,
        ),
        artificial_analysis_url=_aa_url(aa_slug),
    )


DEFAULT_MODEL_OPTIONS: Tuple[DefaultModelOption, ...] = (
    # model=None means "leave unset" — the runtime applies its own defaults
    # (UNIFY_MODEL for actor / SLOW_BRAIN_MODEL for slow brain). Distinct from
    # pinning the same endpoint that currently backs that default.
    _opt(
        model=None,
        reasoning_effort=None,
        label=f"System Default (currently {PLATFORM_DEFAULT_DISPLAY_NAME})",
        # Display credits match the platform default (GPT-5.6 Sol high).
        approx_credits_per_task=475,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="minimax-v3@minimax",
        reasoning_effort=None,
        label="MiniMax-M3",
        approx_credits_per_task=40,
        input_usd_per_m=0.30,
        output_usd_per_m=1.20,
        aa_slug="minimax-m3",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="low",
        label="Gemini 3.1 Pro (low thinking)",
        approx_credits_per_task=100,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="medium",
        label="Gemini 3.1 Pro (medium thinking)",
        approx_credits_per_task=140,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="high",
        label="Gemini 3.1 Pro (high thinking)",
        approx_credits_per_task=200,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="gpt-5.6-luna@openai",
        reasoning_effort="low",
        label="GPT-5.6 Luna (low thinking)",
        approx_credits_per_task=50,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="gpt-5.6-luna@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Luna (medium thinking)",
        approx_credits_per_task=70,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="gpt-5.6-luna@openai",
        reasoning_effort="high",
        label="GPT-5.6 Luna (high thinking)",
        approx_credits_per_task=95,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="gpt-5.6-terra@openai",
        reasoning_effort="low",
        label="GPT-5.6 Terra (low thinking)",
        approx_credits_per_task=120,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="gpt-5.6-terra@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Terra (medium thinking)",
        approx_credits_per_task=170,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="gpt-5.6-terra@openai",
        reasoning_effort="high",
        label="GPT-5.6 Terra (high thinking)",
        approx_credits_per_task=240,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="gpt-5.6-sol@openai",
        reasoning_effort="low",
        label="GPT-5.6 Sol (low thinking)",
        approx_credits_per_task=240,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="gpt-5.6-sol@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Sol (medium thinking)",
        approx_credits_per_task=330,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="gpt-5.6-sol@openai",
        reasoning_effort="high",
        label="GPT-5.6 Sol (high thinking)",
        approx_credits_per_task=475,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="low",
        label="Claude Opus 4.8 (low thinking)",
        approx_credits_per_task=430,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="medium",
        label="Claude Opus 4.8 (medium thinking)",
        approx_credits_per_task=600,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="high",
        label="Claude Opus 4.8 (high thinking)",
        approx_credits_per_task=850,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    # Sonnet 5 has cheaper token rates than Opus 4.8 ($3/$15 vs $5/$25) but a
    # HIGHER per-task cost: it runs ~3x the agent loops and its tokenizer
    # inflates counts ~30%, so Artificial Analysis measures $2.29/task vs
    # Opus 4.8's $1.78. The task estimates deliberately reflect per-task reality;
    # message estimates stay on raw token rates.
    _opt(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="low",
        label="Claude Sonnet 5 (low thinking)",
        approx_credits_per_task=550,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="medium",
        label="Claude Sonnet 5 (medium thinking)",
        approx_credits_per_task=770,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="high",
        label="Claude Sonnet 5 (high thinking)",
        approx_credits_per_task=1100,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="low",
        label="Claude Fable 5 (low thinking)",
        approx_credits_per_task=780,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="medium",
        label="Claude Fable 5 (medium thinking)",
        approx_credits_per_task=1100,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="high",
        label="Claude Fable 5 (high thinking)",
        approx_credits_per_task=1550,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
)

_PLATFORM_DEFAULT_OPTION = next(
    option
    for option in DEFAULT_MODEL_OPTIONS
    if option.model == PLATFORM_DEFAULT_MODEL
    and option.reasoning_effort == PLATFORM_DEFAULT_REASONING_EFFORT
)
DEFAULT_MODEL_OPTIONS = (
    replace(
        DEFAULT_MODEL_OPTIONS[0],
        approx_credits_per_task=_PLATFORM_DEFAULT_OPTION.approx_credits_per_task,
        approx_credits_per_message=_PLATFORM_DEFAULT_OPTION.approx_credits_per_message,
        artificial_analysis_url=_PLATFORM_DEFAULT_OPTION.artificial_analysis_url,
    ),
    *DEFAULT_MODEL_OPTIONS[1:],
)

_VALID_PAIRS = {
    (option.model, option.reasoning_effort)
    for option in DEFAULT_MODEL_OPTIONS
    if option.model is not None
}

# Terra high — used for the slow-brain system-default credit display.
_SLOW_BRAIN_SYSTEM_DEFAULT_CREDITS = next(
    option.approx_credits_per_message
    for option in DEFAULT_MODEL_OPTIONS
    if option.model == PLATFORM_SLOW_BRAIN_MODEL
    and option.reasoning_effort == PLATFORM_SLOW_BRAIN_REASONING_EFFORT
)
_SLOW_BRAIN_SYSTEM_DEFAULT_URL = next(
    option.artificial_analysis_url
    for option in DEFAULT_MODEL_OPTIONS
    if option.model == PLATFORM_SLOW_BRAIN_MODEL
    and option.reasoning_effort == PLATFORM_SLOW_BRAIN_REASONING_EFFORT
)


def is_valid_default_model(
    model: str,
    reasoning_effort: Optional[str],
) -> bool:
    """Return whether (model, reasoning_effort) is a catalog option."""

    return (model, reasoning_effort) in _VALID_PAIRS


# Alias: slow brain uses the same curated pairs as the actor default.
is_valid_slow_brain_model = is_valid_default_model


def list_model_options(
    usage: Literal["actor", "slow_brain"] = "actor",
) -> Tuple[DefaultModelOption, ...]:
    """Return catalog options, with the system-default row labeled for ``usage``."""

    if usage == "actor":
        return DEFAULT_MODEL_OPTIONS

    system_default = replace(
        DEFAULT_MODEL_OPTIONS[0],
        label=f"System Default (currently {PLATFORM_SLOW_BRAIN_DISPLAY_NAME})",
        approx_credits_per_task=DEFAULT_MODEL_OPTIONS[0].approx_credits_per_task,
        approx_credits_per_message=_SLOW_BRAIN_SYSTEM_DEFAULT_CREDITS,
        artificial_analysis_url=_SLOW_BRAIN_SYSTEM_DEFAULT_URL,
    )
    return (system_default, *DEFAULT_MODEL_OPTIONS[1:])
