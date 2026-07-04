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

LEARNING_EXPENSES_GUIDANCE_TITLE = "Monthly bank export spend rules"
LEARNING_EXPENSES_FUNCTION_NAME = "compute_monthly_spend_from_bank_exports"


def learning_expenses_storage_check_nudge() -> str:
    """Explicit StorageCheck mandate for the learning correction interjection."""
    return (
        "StorageCheck memoization (for the post-act review loop — do NOT call "
        "GuidanceManager or FunctionManager store tools in the doing loop): "
        f"persist Guidance titled {LEARNING_EXPENSES_GUIDANCE_TITLE!r} with the "
        "user's correction rule (skip INTERNAL XFER rows; sum remaining "
        "outflows; net REFUND rows against spend) and Function "
        f"{LEARNING_EXPENSES_FUNCTION_NAME!r} for the corrected monthly spend "
        "pipeline from checking+card CSV exports."
    )


def learning_expenses_stop_act_for_storage_rule() -> str:
    """CM must end the persist act after the improved deliverable to run StorageCheck."""
    return (
        "After sending the improved deliverable tagged "
        "onboarding_learning_phase=improved, call stop_* on the running "
        "persist act in the SAME turn — StorageCheck only starts once the "
        "persist session ends, not while it sits in awaiting_input. Tell the "
        "user in plain language that you are stopping the action so Brain can "
        "save their rule (for example: stopping it now so your correction gets "
        "saved), then invoke stop_* before inviting February."
    )


def learning_expenses_user_facing_voice() -> str:
    """Plain-language rules for Learning demo chat messages (non-technical audience)."""
    return (
        "User-facing voice: the audience is non-technical. Keep every learning-demo "
        "chat message short and scannable — a headline dollar total plus one or two "
        "plain sentences. Do NOT send markdown tables, line-by-line row breakdowns, "
        "disposition/contribution columns, or accounting jargon (gross outflows, "
        "netted, phantom spending, rule 1/rule 2). The CSVs are attachments for "
        "anyone curious; do not recite every row in chat. "
        "Opening arc: at most five short bullets; casual tone, not a compliance brief. "
        "Attachment captions: one sentence each (what the file is; mention the "
        "checking↔card transfer trap in plain English). "
        "First-attempt deliverable: state the naive total, then one sentence on "
        "the mistake (double-counted the internal transfer between checking and "
        "card), then the exact correction text to paste — nothing else. "
        "Improved deliverable: state the corrected total, one sentence on what "
        "changed (skipped transfers, counted refunds), optionally one contrast "
        "vs the naive total — say you are stopping the action so Brain can save "
        "their rule, then stop_* the persist act in the same turn, then "
        "Brain/StorageCheck nudge and invite February. "
        "Replay deliverable: corrected total for the new month in one line."
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
        "Share two January bank CSVs (checking + card).",
        "Run a naive pass that double-counts the internal transfer.",
        "You send a short correction (I'll suggest exact text).",
        "I revise, stop the action so Brain saves your rule and a reusable pipeline.",
        "Ask for February when ready — I'll replay using what we learned.",
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
        f"When sending `{checking}`: one sentence — January checking export; "
        "includes a transfer to the card that sets up the double-count demo."
    )


def learning_expenses_card_attachment_description() -> str:
    """What to tell the user when sending the card CSV."""
    card = _card_month_n_path()
    return (
        f"When sending `{card}`: one sentence — January card export; the matching "
        "transfer from checking appears here too (that is the trap)."
    )


def learning_expenses_opening_script_guidance() -> str:
    """Conversational opening tone for the first message after the row click."""
    arc = " → ".join(learning_expenses_intro_arc_lines())
    return (
        "Open casually (for example: ok, this is the learning demo — here is the "
        "plan). Preview the arc in at most five short bullets before attachments: "
        f"{arc}. "
        f"{learning_expenses_contrivance_acknowledgment()} "
        f"{learning_expenses_user_facing_voice()}"
    )
