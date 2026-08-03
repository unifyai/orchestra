"""Scenario facts for Learning onboarding narration in Orchestra.

Row descriptions mirror the bundled CSV fixtures under
``unify/assets/onboarding/learning/billsplit/`` in the Unity repo so intro
copy cannot drift from the files the twin provisions at runtime.
"""

from __future__ import annotations

LEARNING_BILLSPLIT_BASE_DIR = "onboarding/learning/billsplit"
LEARNING_BILLSPLIT_FRIDAY_RECEIPT = "dinner-receipt-friday.csv"
LEARNING_BILLSPLIT_SATURDAY_RECEIPT = "dinner-receipt-saturday.csv"
LEARNING_BILLSPLIT_SCENARIO_ID = "billsplit-dinner"

LEARNING_BILLSPLIT_USER_CORRECTION_TEXT = (
    "Sam doesn't drink — don't split alcohol across people who didn't drink."
)

LEARNING_BILLSPLIT_GUIDANCE_TITLE = "Bill splitting rules"
LEARNING_BILLSPLIT_KNOWLEDGE_FACT = "Sam doesn't drink alcohol"
LEARNING_BILLSPLIT_FUNCTION_NAME = "split_dinner_bill"


def learning_billsplit_storage_check_nudge() -> str:
    """Explicit StorageCheck mandate for the learning correction interjection."""
    return (
        "StorageCheck memoization (for the post-act review loop — do NOT call "
        "GuidanceManager or KnowledgeManager or FunctionManager store tools in "
        f"the doing loop): persist Guidance titled {LEARNING_BILLSPLIT_GUIDANCE_TITLE!r} "
        "with the user's correction rule (only alcohol lines are excluded from "
        "the split — food, dessert, and non-alcoholic drinks still split evenly "
        f"across everyone), Knowledge that {LEARNING_BILLSPLIT_KNOWLEDGE_FACT!r}, "
        f"and Function {LEARNING_BILLSPLIT_FUNCTION_NAME!r} for the corrected "
        "bill-splitting computation."
    )


def learning_billsplit_stop_act_for_storage_rule() -> str:
    """CM must end the persist act after the improved deliverable to run StorageCheck."""
    return (
        "After sending the improved deliverable, call stop_* on the running "
        "persist act in the SAME turn — StorageCheck only starts once the "
        "persist session ends, not while it sits in awaiting_input. Tell the "
        "user in plain language that you are stopping the action so Brain can "
        "save their rule (for example: stopping this run now so Brain can save "
        "your rule). Do not invite Saturday's dinner yet — that invite is part "
        "of the save announcement, sent only once the save actually completes."
    )


def learning_billsplit_user_facing_voice() -> str:
    """Plain-language rules for Learning demo chat messages (non-technical audience)."""
    return (
        "User-facing voice: the audience is non-technical. Keep every learning-demo "
        "chat message short and scannable — a headline dollar total plus one or two "
        "plain sentences. Do NOT send markdown tables, line-by-line row breakdowns, "
        "or jargon (pools, pro-rata, allocation). The receipts are attachments for "
        "anyone curious; do not recite every line item in chat. "
        "Opening message: first teach Brain in 3–4 short concept bullets — "
        "corrections stick (learning), Guidance is my playbook for how to work, "
        "Functions are reusable skills, and together they mean less re-explaining "
        "on similar tasks. Keep concept lines domain-agnostic (no receipt/split "
        "jargon). Then announce the trick plainly and point them at the Actions tab. "
        "Attachment captions: one sentence each, naming the attendees for that dinner. "
        "Naive deliverable: state the naive total, then one sentence owning the "
        "mistake (Sam got charged for alcohol they never touched), then the exact "
        "correction text to paste — nothing else. "
        "Corrected deliverable: state the corrected total, one sentence on what "
        "changed, say you are stopping the run so Brain can save the rule, then "
        "stop_* the persist act in the same turn — no invite yet. "
        "Save announcement (separate, proactive message once the save completes): "
        "cite what was actually stored (the rule, the Sam fact, the skill), point "
        "at the Brain tab, and invite Saturday's dinner as the test. "
        "Replay deliverable: the corrected total for Saturday in one line, plus a "
        "short note that nobody reminded me about Sam this time."
    )


LEARNING_BILLSPLIT_NAIVE_MISTAKE_DESCRIPTION = (
    "split Friday's total evenly across all four attendees, alcohol lines "
    "included, so Sam — who doesn't drink — gets charged for wine and beer "
    "they never touched"
)

LEARNING_BILLSPLIT_REPLAY_HINT = (
    "Once the user asks for Saturday's dinner, run the saved bill-splitting "
    f"skill zero-shot on {LEARNING_BILLSPLIT_BASE_DIR}/{LEARNING_BILLSPLIT_SATURDAY_RECEIPT} "
    "— five attendees this time, Sam returns, no reminder given."
)


def _friday_receipt_path() -> str:
    return f"{LEARNING_BILLSPLIT_BASE_DIR}/{LEARNING_BILLSPLIT_FRIDAY_RECEIPT}"


def _saturday_receipt_path() -> str:
    return f"{LEARNING_BILLSPLIT_BASE_DIR}/{LEARNING_BILLSPLIT_SATURDAY_RECEIPT}"


def learning_billsplit_deliverable_handoff_rule() -> str:
    """Explicit post-act deliverable rule for the learning demo script."""
    return (
        "Deliverable handoff (non-negotiable): the moment a demo act run "
        "completes and returns its result, my SAME turn must send that result "
        "to the user as a unify_message. Responding to a completed demo act "
        "with a bare wait and no message is a script violation — the tutorial "
        "stalls and the user sees nothing. This applies after every act pass: "
        "the naive first attempt, the corrected revision, and the Saturday "
        "replay."
    )


def learning_billsplit_intro_arc_lines() -> tuple[str, ...]:
    """High-level arc to preview before sending any files."""
    return (
        "Send Friday's dinner receipt for four people, including Sam.",
        "Run a naive even split that includes alcohol, on purpose overcharging Sam.",
        "You send a short correction (I'll suggest exact text).",
        "I revise, stop the run so Brain can save your rule, the Sam fact, and a "
        "reusable skill.",
        "Once Brain finishes saving, I'll invite you to test me on Saturday's "
        "dinner.",
    )


def learning_billsplit_concepts_intro_lines() -> tuple[str, ...]:
    """Plain-language Brain concepts to teach before the hands-on demo steps."""
    return (
        "Learning — when you correct me, the fix sticks beyond this chat; "
        "you shouldn't have to repeat yourself on similar work.",
        "Guidance (Brain → Guidance) — my playbook for *how* to work with you: "
        "rules, preferences, steps, and pitfalls (the way we do things here).",
        "Functions (Brain → Functions) — skills I pick up for *what* I can do "
        "again: concrete workflows I reuse when a similar task comes up.",
        "How they fit — after I finish work, I review what happened and save "
        "worthwhile rules and skills to Brain, so similar tasks start smarter.",
    )


def learning_billsplit_concepts_opening_guidance() -> str:
    """Instruct the CM to teach Brain concepts before the demo plan."""
    concepts = " | ".join(learning_billsplit_concepts_intro_lines())
    return (
        "Before the step-by-step plan, teach the intuition behind learning, "
        "Guidance, and Functions — the onboarding goal is day-to-day understanding "
        "(corrections stick, playbooks vs skills, less re-explaining), not demo "
        "mechanics. Cover these in plain language (short bullets or sentences, "
        f"non-technical, domain-agnostic): {concepts}"
    )


def learning_billsplit_contrivance_acknowledgment() -> str:
    """Tone guidance: own that the demo is staged."""
    return (
        "Announce the trick plainly and up front — say you're going to get the "
        "first split wrong on purpose and their job is to catch you, then tell "
        "them to open the Actions tab so they can watch you work. Owning the "
        "trick like this is fine: the point is showing how one correction "
        "becomes a durable rule, a durable fact, and a reusable skill."
    )


def learning_billsplit_friday_attachment_description() -> str:
    """What to tell the user when sending the Friday receipt."""
    friday = _friday_receipt_path()
    return (
        f"When sending `{friday}`: one sentence — Friday's dinner, naming the "
        "four attendees (you, Sam, Priya, Jordan) and asking me to split it."
    )


def learning_billsplit_saturday_attachment_description() -> str:
    """What to tell the user when sending the Saturday receipt."""
    saturday = _saturday_receipt_path()
    return (
        f"When sending `{saturday}`: one sentence — Saturday's dinner, naming "
        "the five attendees this time (you, Sam, Priya, Marcus, Dana), noting "
        "Sam came again."
    )


def learning_billsplit_opening_script_guidance() -> str:
    """Conversational opening tone for the first message after the row click."""
    concepts = " → ".join(learning_billsplit_concepts_intro_lines())
    arc = " → ".join(learning_billsplit_intro_arc_lines())
    return (
        "Open casually (for example: ok, this is the learning demo). "
        f"First teach the concepts: {concepts}. "
        "Then preview the hands-on plan in at most five short bullets before "
        f"attachments: {arc}. "
        f"{learning_billsplit_contrivance_acknowledgment()} "
        f"{learning_billsplit_user_facing_voice()}"
    )
