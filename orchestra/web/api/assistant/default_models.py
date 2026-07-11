"""Curated catalog of per-assistant default LLM options.

Each option pairs a unillm ``model@provider`` endpoint with a reasoning-effort
level. Only multimodal models (native image input) are eligible, because the
assistant runtime routes screenshots and other image content through its
default model's call sites.

An option with ``reasoning_effort=None`` leaves the runtime's per-call-site
effort levels untouched; a concrete effort overrides them wherever the default
model is used.

``approx_credits_per_task`` is a display-only, order-of-magnitude estimate of
what one typical assistant task costs at that option, in customer-facing
credits (1 USD of billed provider cost = 400 credits, billed at a 1.2x margin
on provider rates). The high-effort figures are anchored to Artificial
Analysis's "Cost per Intelligence Index Task" measurements (heavyweight
agentic benchmark tasks at max/xhigh effort); medium and low efforts are
scaled to roughly 70% and 50% of the high anchor. Real tasks vary by an order
of magnitude either way — treat these as relative price signals, not quotes.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

PLATFORM_DEFAULT_MODEL = "minimax-v3@minimax"

_AA_MODELS_BASE_URL = "https://artificialanalysis.ai/models"


@dataclass(frozen=True)
class DefaultModelOption:
    model: str
    reasoning_effort: Optional[str]
    label: str
    approx_credits_per_task: int
    artificial_analysis_url: str


def _aa_url(slug: str) -> str:
    return f"{_AA_MODELS_BASE_URL}/{slug}"


DEFAULT_MODEL_OPTIONS: Tuple[DefaultModelOption, ...] = (
    DefaultModelOption(
        model=PLATFORM_DEFAULT_MODEL,
        reasoning_effort=None,
        label="MiniMax-M3 (platform default)",
        approx_credits_per_task=40,
        artificial_analysis_url=_aa_url("minimax-m3"),
    ),
    DefaultModelOption(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="low",
        label="Gemini 3.1 Pro (low thinking)",
        approx_credits_per_task=100,
        artificial_analysis_url=_aa_url("gemini-3-1-pro-preview"),
    ),
    DefaultModelOption(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="medium",
        label="Gemini 3.1 Pro (medium thinking)",
        approx_credits_per_task=140,
        artificial_analysis_url=_aa_url("gemini-3-1-pro-preview"),
    ),
    DefaultModelOption(
        model="gemini-3-pro@vertex-ai",
        reasoning_effort="high",
        label="Gemini 3.1 Pro (high thinking)",
        approx_credits_per_task=200,
        artificial_analysis_url=_aa_url("gemini-3-1-pro-preview"),
    ),
    DefaultModelOption(
        model="gpt-5.6-luna@openai",
        reasoning_effort="low",
        label="GPT-5.6 Luna (low thinking)",
        approx_credits_per_task=50,
        artificial_analysis_url=_aa_url("gpt-5-6-luna"),
    ),
    DefaultModelOption(
        model="gpt-5.6-luna@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Luna (medium thinking)",
        approx_credits_per_task=70,
        artificial_analysis_url=_aa_url("gpt-5-6-luna"),
    ),
    DefaultModelOption(
        model="gpt-5.6-luna@openai",
        reasoning_effort="high",
        label="GPT-5.6 Luna (high thinking)",
        approx_credits_per_task=95,
        artificial_analysis_url=_aa_url("gpt-5-6-luna"),
    ),
    DefaultModelOption(
        model="gpt-5.6-terra@openai",
        reasoning_effort="low",
        label="GPT-5.6 Terra (low thinking)",
        approx_credits_per_task=120,
        artificial_analysis_url=_aa_url("gpt-5-6-terra"),
    ),
    DefaultModelOption(
        model="gpt-5.6-terra@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Terra (medium thinking)",
        approx_credits_per_task=170,
        artificial_analysis_url=_aa_url("gpt-5-6-terra"),
    ),
    DefaultModelOption(
        model="gpt-5.6-terra@openai",
        reasoning_effort="high",
        label="GPT-5.6 Terra (high thinking)",
        approx_credits_per_task=240,
        artificial_analysis_url=_aa_url("gpt-5-6-terra"),
    ),
    DefaultModelOption(
        model="gpt-5.6-sol@openai",
        reasoning_effort="low",
        label="GPT-5.6 Sol (low thinking)",
        approx_credits_per_task=240,
        artificial_analysis_url=_aa_url("gpt-5-6-sol"),
    ),
    DefaultModelOption(
        model="gpt-5.6-sol@openai",
        reasoning_effort="medium",
        label="GPT-5.6 Sol (medium thinking)",
        approx_credits_per_task=330,
        artificial_analysis_url=_aa_url("gpt-5-6-sol"),
    ),
    DefaultModelOption(
        model="gpt-5.6-sol@openai",
        reasoning_effort="high",
        label="GPT-5.6 Sol (high thinking)",
        approx_credits_per_task=475,
        artificial_analysis_url=_aa_url("gpt-5-6-sol"),
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="low",
        label="Claude Opus 4.8 (low thinking)",
        approx_credits_per_task=430,
        artificial_analysis_url=_aa_url("claude-opus-4-8"),
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="medium",
        label="Claude Opus 4.8 (medium thinking)",
        approx_credits_per_task=600,
        artificial_analysis_url=_aa_url("claude-opus-4-8"),
    ),
    DefaultModelOption(
        model="claude-4.8-opus@anthropic",
        reasoning_effort="high",
        label="Claude Opus 4.8 (high thinking)",
        approx_credits_per_task=850,
        artificial_analysis_url=_aa_url("claude-opus-4-8"),
    ),
    # Sonnet 5 has cheaper token rates than Opus 4.8 ($3/$15 vs $5/$25) but a
    # HIGHER per-task cost: it runs ~3x the agent loops and its tokenizer
    # inflates counts ~30%, so Artificial Analysis measures $2.29/task vs
    # Opus 4.8's $1.78. The estimates deliberately reflect per-task reality.
    DefaultModelOption(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="low",
        label="Claude Sonnet 5 (low thinking)",
        approx_credits_per_task=550,
        artificial_analysis_url=_aa_url("claude-sonnet-5"),
    ),
    DefaultModelOption(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="medium",
        label="Claude Sonnet 5 (medium thinking)",
        approx_credits_per_task=770,
        artificial_analysis_url=_aa_url("claude-sonnet-5"),
    ),
    DefaultModelOption(
        model="claude-sonnet-5@anthropic",
        reasoning_effort="high",
        label="Claude Sonnet 5 (high thinking)",
        approx_credits_per_task=1100,
        artificial_analysis_url=_aa_url("claude-sonnet-5"),
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="low",
        label="Claude Fable 5 (low thinking)",
        approx_credits_per_task=780,
        artificial_analysis_url=_aa_url("claude-fable-5"),
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="medium",
        label="Claude Fable 5 (medium thinking)",
        approx_credits_per_task=1100,
        artificial_analysis_url=_aa_url("claude-fable-5"),
    ),
    DefaultModelOption(
        model="claude-fable-5@anthropic",
        reasoning_effort="high",
        label="Claude Fable 5 (high thinking)",
        approx_credits_per_task=1550,
        artificial_analysis_url=_aa_url("claude-fable-5"),
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
