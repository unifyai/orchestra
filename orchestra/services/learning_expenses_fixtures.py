"""Scenario facts for Learning onboarding narration in Orchestra.

Row descriptions mirror the bundled CSV fixtures under
``unify/assets/onboarding/learning/expenses/`` in the Unity repo so intro
copy cannot drift from the files the twin provisions at runtime.
"""

from __future__ import annotations

LEARNING_EXPENSES_BASE_DIR = "onboarding/learning/expenses"
LEARNING_EXPENSES_MONTH_N = "2026-01"
LEARNING_EXPENSES_MONTH_N_PLUS_1 = "2026-02"
LEARNING_EXPENSES_SCENARIO_ID = "expenses-etl"

LEARNING_EXPENSES_USER_CORRECTION_TEXT = (
    "Exclude internal transfer rows and net refunds against spend when computing "
    "monthly spend."
)

LEARNING_EXPENSES_NAIVE_MISTAKE_DESCRIPTION = (
    "sum every outflow as spend, add abs(Amount) again for each INTERNAL XFER "
    "row on either file (including card-side credits) so the transfer is "
    "double-counted, and ignore refunds"
)

LEARNING_EXPENSES_REPLAY_HINT = (
    "Run the stored pipeline on the next month's bank exports "
    f"({LEARNING_EXPENSES_BASE_DIR}/ month N+1 files) once the user asks."
)


def _checking_month_n_path() -> str:
    return f"{LEARNING_EXPENSES_BASE_DIR}/checking-{LEARNING_EXPENSES_MONTH_N}.csv"


def _card_month_n_path() -> str:
    return f"{LEARNING_EXPENSES_BASE_DIR}/card-{LEARNING_EXPENSES_MONTH_N}.csv"


def learning_expenses_deliverable_handoff_rule() -> str:
    """Explicit post-act deliverable rule for the learning demo script."""
    return (
        "Deliverable handoff (non-negotiable): the moment a demo act run "
        "completes and returns its result, my SAME turn must send that result "
        "to the user as a unify_message tagged with the current "
        "onboarding_learning_phase (first_attempt, improved, or replay). "
        "Responding to a completed demo act with a bare wait and no tagged "
        "message is a script violation — the tutorial stalls and the user "
        "sees nothing. This applies after every act pass: the first naive "
        "attempt, the improved revision, and the month-N+1 replay."
    )


def learning_expenses_intro_arc_lines() -> tuple[str, ...]:
    """High-level arc to preview before sending any files."""
    return (
        "Share the January bank exports (one CSV per message) so they can see the data.",
        "Run a deliberately naive first pass that double-counts the internal transfer.",
        "Wait for them to send the correction in their own words (suggest exact text).",
        "Revise, store the rule as Guidance and the pipeline as a Function.",
        "Point them to the Brain rail Guidance and Functions sections themselves.",
        f"Wait until they ask for February; replay on month-N+1 ({LEARNING_EXPENSES_MONTH_N_PLUS_1}) "
        "to prove the learning stuck.",
    )


def learning_expenses_contrivance_acknowledgment() -> str:
    """Tone guidance: own that the demo is staged."""
    return (
        "Acknowledge this scenario is deliberately contrived — say so plainly "
        "and that is fine: the point is to show how one correction becomes "
        "durable Guidance and a reusable Function for similar work later."
    )


def learning_expenses_checking_attachment_description() -> str:
    """What to tell the user when sending the checking CSV."""
    checking = _checking_month_n_path()
    return (
        f"When sending `{checking}` (January checking account): say it is five "
        "rows — grocery (Whole Foods), payroll deposit (a credit), an INTERNAL "
        "XFER TO VISA that moves money to the card, a utilities bill, and an "
        "AMZN REFUND (positive amount). The transfer pair with the card file "
        "is what makes the naive pass go wrong."
    )


def learning_expenses_card_attachment_description() -> str:
    """What to tell the user when sending the card CSV."""
    card = _card_month_n_path()
    return (
        f"When sending `{card}` (January card statement): say it is four rows — "
        "an Amazon purchase, INTERNAL XFER FROM CHK (the matching credit from "
        "checking), gas, and a cryptic coffee-shop merchant string. Mention "
        "the internal transfer shows up on both files."
    )


def learning_expenses_opening_script_guidance() -> str:
    """Conversational opening tone for the first message after the row click."""
    arc = " → ".join(learning_expenses_intro_arc_lines())
    return (
        "Open like a person walking them through the demo, not a compliance "
        "brief — casual and direct (for example: ok so this is the learning "
        "demo; here is what we are going to do). Preview the full arc before "
        "any attachments: "
        f"{arc}. "
        f"{learning_expenses_contrivance_acknowledgment()}"
    )
