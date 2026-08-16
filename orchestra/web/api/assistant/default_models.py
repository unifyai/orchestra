"""Curated catalog of per-assistant LLM options (actor default + slow brain).

Each option pairs a unillm ``model@provider`` endpoint with a reasoning-effort
level. Only multimodal models (native image input) are eligible, because the
assistant runtime routes screenshots and other image content through these
call sites.

An option with ``reasoning_effort=None`` leaves the runtime's per-call-site
effort levels untouched; a concrete effort overrides them wherever that model
is used.

Credit estimates (display-only; 1 USD provider cost = 400 credits):

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

PLATFORM_DEFAULT_MODEL = "openai/gpt-5.6-sol@openrouter"
PLATFORM_DEFAULT_REASONING_EFFORT = "high"
PLATFORM_DEFAULT_DISPLAY_NAME = "GPT-5.6 Sol"

# Matches Unify's UNIFY_CONVERSATION_SLOW_BRAIN_* defaults.
PLATFORM_SLOW_BRAIN_MODEL = "openai/gpt-5.6-terra@openrouter"
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
_CREDITS_PER_USD = 400


@dataclass(frozen=True)
class DefaultModelOption:
    model: Optional[str]
    reasoning_effort: Optional[str]
    label: str
    # None when Artificial Analysis has not published a Cost per Intelligence
    # Index Task figure for the model yet. Token rates are shown instead of a
    # task estimate invented from token math, which would not be comparable
    # with the anchored figures on the other rows.
    approx_credits_per_task: Optional[int]
    approx_credits_per_message: int
    artificial_analysis_url: str
    input_usd_per_m: float
    output_usd_per_m: float


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
    return max(1, round(usd * _CREDITS_PER_USD))


def credits_per_message_from_token_costs(
    input_cost_per_token: Optional[float],
    output_cost_per_token: Optional[float],
    reasoning_effort: Optional[str] = None,
) -> Optional[int]:
    """Message credits for a catalog model, from its live per-token rates.

    Catalog models have no Artificial Analysis task anchor, so only the
    token-derived message estimate is meaningful for them; per-task cost stays
    unknown rather than guessed.
    """

    if input_cost_per_token is None or output_cost_per_token is None:
        return None
    return _msg_credits(
        input_cost_per_token * 1_000_000,
        output_cost_per_token * 1_000_000,
        reasoning_effort,
    )


def _opt(
    *,
    model: Optional[str],
    reasoning_effort: Optional[str],
    label: str,
    approx_credits_per_task: Optional[int],
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
        input_usd_per_m=input_usd_per_m,
        output_usd_per_m=output_usd_per_m,
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
        approx_credits_per_task=396,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="minimax-v3@minimax",
        reasoning_effort=None,
        label="MiniMax-M3",
        approx_credits_per_task=33,
        input_usd_per_m=0.30,
        output_usd_per_m=1.20,
        aa_slug="minimax-m3",
    ),
    # Kimi K3 launches with max thinking only (low/high effort modes TBD).
    # AA Intelligence Index ~$0.95/task → ~380 credits.
    _opt(
        model="kimi-k3@moonshotai",
        reasoning_effort=None,
        label="Kimi K3",
        approx_credits_per_task=380,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="kimi-k3",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="low",
        label="Gemini 3.1 Pro (low thinking)",
        approx_credits_per_task=83,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="medium",
        label="Gemini 3.1 Pro (medium thinking)",
        approx_credits_per_task=117,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="high",
        label="Gemini 3.1 Pro (high thinking)",
        approx_credits_per_task=167,
        input_usd_per_m=1.25,
        output_usd_per_m=10.0,
        aa_slug="gemini-3-1-pro-preview",
    ),
    _opt(
        model="openai/gpt-5.6-luna@openrouter",
        reasoning_effort="low",
        label="GPT-5.6 Luna (low thinking)",
        approx_credits_per_task=42,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="openai/gpt-5.6-luna@openrouter",
        reasoning_effort="medium",
        label="GPT-5.6 Luna (medium thinking)",
        approx_credits_per_task=58,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="openai/gpt-5.6-luna@openrouter",
        reasoning_effort="high",
        label="GPT-5.6 Luna (high thinking)",
        approx_credits_per_task=79,
        input_usd_per_m=1.0,
        output_usd_per_m=6.0,
        aa_slug="gpt-5-6-luna",
    ),
    _opt(
        model="openai/gpt-5.6-terra@openrouter",
        reasoning_effort="low",
        label="GPT-5.6 Terra (low thinking)",
        approx_credits_per_task=100,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="openai/gpt-5.6-terra@openrouter",
        reasoning_effort="medium",
        label="GPT-5.6 Terra (medium thinking)",
        approx_credits_per_task=142,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="openai/gpt-5.6-terra@openrouter",
        reasoning_effort="high",
        label="GPT-5.6 Terra (high thinking)",
        approx_credits_per_task=200,
        input_usd_per_m=2.50,
        output_usd_per_m=15.0,
        aa_slug="gpt-5-6-terra",
    ),
    _opt(
        model="openai/gpt-5.6-sol@openrouter",
        reasoning_effort="low",
        label="GPT-5.6 Sol (low thinking)",
        approx_credits_per_task=200,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="openai/gpt-5.6-sol@openrouter",
        reasoning_effort="medium",
        label="GPT-5.6 Sol (medium thinking)",
        approx_credits_per_task=275,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="openai/gpt-5.6-sol@openrouter",
        reasoning_effort="high",
        label="GPT-5.6 Sol (high thinking)",
        approx_credits_per_task=396,
        input_usd_per_m=5.0,
        output_usd_per_m=30.0,
        aa_slug="gpt-5-6-sol",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="low",
        label="Claude Opus 4.8 (low thinking)",
        approx_credits_per_task=358,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="medium",
        label="Claude Opus 4.8 (medium thinking)",
        approx_credits_per_task=500,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    _opt(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="high",
        label="Claude Opus 4.8 (high thinking)",
        approx_credits_per_task=708,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-4-8",
    ),
    # Opus 5: same $5/$25 token rates as Opus 4.8; AA ~$2.03/task at max
    # effort → ~812 credits. Medium/low scale to ~70%/50% of the high anchor.
    _opt(
        model="claude-opus-5@anthropic",
        reasoning_effort="low",
        label="Claude Opus 5 (low thinking)",
        approx_credits_per_task=408,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-5",
    ),
    _opt(
        model="claude-opus-5@anthropic",
        reasoning_effort="medium",
        label="Claude Opus 5 (medium thinking)",
        approx_credits_per_task=567,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-5",
    ),
    _opt(
        model="claude-opus-5@anthropic",
        reasoning_effort="high",
        label="Claude Opus 5 (high thinking)",
        approx_credits_per_task=812,
        input_usd_per_m=5.0,
        output_usd_per_m=25.0,
        aa_slug="claude-opus-5",
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
        approx_credits_per_task=458,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="medium",
        label="Claude Sonnet 5 (medium thinking)",
        approx_credits_per_task=642,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="high",
        label="Claude Sonnet 5 (high thinking)",
        approx_credits_per_task=917,
        input_usd_per_m=3.0,
        output_usd_per_m=15.0,
        aa_slug="claude-sonnet-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="low",
        label="Claude Fable 5 (low thinking)",
        approx_credits_per_task=650,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="medium",
        label="Claude Fable 5 (medium thinking)",
        approx_credits_per_task=917,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
    _opt(
        model="claude-fable-5@anthropic",
        reasoning_effort="high",
        label="Claude Fable 5 (high thinking)",
        approx_credits_per_task=1292,
        input_usd_per_m=10.0,
        output_usd_per_m=50.0,
        aa_slug="claude-fable-5",
    ),
    # Recent releases Artificial Analysis benchmarks but has not yet published a
    # per-task cost for. They carry no task estimate, and the runtime's own
    # per-call-site effort levels apply, as with MiniMax-M3 and Kimi K3 above.
    _opt(
        model="x-ai/grok-4.5@openrouter",
        reasoning_effort=None,
        label="Grok 4.5",
        approx_credits_per_task=None,
        input_usd_per_m=2.00,
        output_usd_per_m=6.00,
        aa_slug="grok-4-5",
    ),
    _opt(
        model="google/gemini-3.6-flash@openrouter",
        reasoning_effort=None,
        label="Gemini 3.6 Flash",
        approx_credits_per_task=None,
        input_usd_per_m=1.50,
        output_usd_per_m=7.50,
        aa_slug="gemini-3-6-flash",
    ),
    _opt(
        model="google/gemini-3.5-flash-lite@openrouter",
        reasoning_effort=None,
        label="Gemini 3.5 Flash Lite",
        approx_credits_per_task=None,
        input_usd_per_m=0.30,
        output_usd_per_m=2.50,
        aa_slug="gemini-3-5-flash-lite",
    ),
    _opt(
        model="meta/muse-spark-1.1@openrouter",
        reasoning_effort=None,
        label="Muse Spark 1.1",
        approx_credits_per_task=None,
        input_usd_per_m=1.25,
        output_usd_per_m=4.25,
        aa_slug="muse-spark-1-1",
    ),
    _opt(
        model="thinkingmachines/inkling@openrouter",
        reasoning_effort=None,
        label="Inkling",
        approx_credits_per_task=None,
        input_usd_per_m=1.00,
        output_usd_per_m=4.05,
        aa_slug="inkling",
    ),
    _opt(
        model="thinkingmachines/inkling-small@openrouter",
        reasoning_effort=None,
        label="Inkling Small",
        approx_credits_per_task=None,
        input_usd_per_m=0.50,
        output_usd_per_m=1.20,
        aa_slug="inkling-small",
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

_VALID_EFFORTS = {None, "low", "medium", "high"}

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
    *,
    require_tools: bool = False,
) -> bool:
    """Return whether (model, reasoning_effort) may be set on an assistant.

    Accepts curated catalog pairs, or any OpenRouter catalog endpoint that
    meets the multimodal (and optional tools) capability policy.
    """

    if (model, reasoning_effort) in _VALID_PAIRS:
        return True
    if reasoning_effort not in _VALID_EFFORTS:
        return False

    from orchestra.services.openrouter_catalog import (
        get_model,
        is_eligible_assistant_model,
        parse_openrouter_endpoint,
    )

    ok, _reason = is_eligible_assistant_model(model, require_tools=require_tools)
    if not ok:
        return False
    model_id = parse_openrouter_endpoint(model)
    if model_id is None:
        return False
    info = get_model(model_id) or {}
    if reasoning_effort is not None and not info.get("supports_reasoning"):
        return False
    return True


def is_valid_slow_brain_model(
    model: str,
    reasoning_effort: Optional[str],
) -> bool:
    """Slow brain shares the multimodal catalog; tools are not required."""

    return is_valid_default_model(model, reasoning_effort, require_tools=False)


def list_model_options(
    usage: Literal["actor", "slow_brain"] = "actor",
) -> Tuple[DefaultModelOption, ...]:
    """Return curated recommended options, labeled for ``usage``."""

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
