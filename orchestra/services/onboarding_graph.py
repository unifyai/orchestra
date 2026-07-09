"""Canonical Coordinator onboarding graph — single source of truth.

This module owns the *structure* of Coordinator onboarding: the ordered
set of steps, how they depend on one another, which channel each belongs
to, whether the user can defer it, and the ready-to-use copy the
Coordinator should say to nudge the user toward each one.

It deliberately consolidates what used to be scattered across three
places:
  - Console's ``ONBOARDING_CHECKLIST`` (titles, phases, ``depends_on``).
  - Unity's ``_VOICE_ONBOARDING_STEP_SUGGESTIONS`` /
    ``_VOICE_ONBOARDING_TRIGGER_REPLY_STEPS`` (spoken nudge copy + the
    trigger→reply pairing).
  - The linear ``DERIVABLE_ONBOARDING_STEPS`` tuple in
    ``coordinator_service`` (which steps are server-derivable).

Both Unity brains and the Console checklist consume a rendering computed
from this graph (see ``coordinator_service.compute_onboarding_render``)
so nothing downstream has to re-derive "what's done / what's next".

Dependency levels mirror the original Console semantics:
  - ``ADDRESSED`` (0): the dependency unlocks this step once it is
    *resolved* — completed OR skipped/deferred.
  - ``COMPLETED`` (1): the dependency must be genuinely completed; a
    skip does not unlock the dependent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestra.services.learning_expenses_fixtures import (
    LEARNING_EXPENSES_NAIVE_MISTAKE_DESCRIPTION,
    LEARNING_EXPENSES_REPLAY_HINT,
    LEARNING_EXPENSES_SCENARIO_ID,
    LEARNING_EXPENSES_USER_CORRECTION_TEXT,
    learning_expenses_card_attachment_description,
    learning_expenses_checking_attachment_description,
    learning_expenses_concepts_opening_guidance,
    learning_expenses_contrivance_acknowledgment,
    learning_expenses_deliverable_handoff_rule,
    learning_expenses_intro_arc_lines,
    learning_expenses_opening_script_guidance,
    learning_expenses_stop_act_for_storage_rule,
    learning_expenses_storage_check_nudge,
    learning_expenses_user_facing_voice,
)

ADDRESSED = 0
COMPLETED = 1


@dataclass(frozen=True)
class OnboardingEventSpec:
    """Structured event payload Console can dispatch without knowing semantics."""

    event_type: str
    message: str
    subtype: str
    details: dict[str, Any]


@dataclass(frozen=True)
class OnboardingStep:
    """One node in the onboarding graph.

    ``derivable`` marks non-trigger steps whose completion Orchestra reads
    from durable domain state (``derive_onboarding_progress``). Trigger
    rows use ``paired_reply`` for dependency modelling, while completion is
    derived from assistant-authored outbound transcript evidence.
    """

    id: str
    title: str
    phase: str
    kind: str
    depends_on: dict[str, int]
    can_skip: bool
    derivable: bool
    channel: str | None = None
    paired_reply: str | None = None
    nudge_chat: str = ""
    nudge_voice: str = ""
    event: OnboardingEventSpec | None = None
    # Workspace providers this step applies to. Empty means every provider
    # (the common case). A non-empty tuple marks a provider-exclusive step
    # (e.g. Microsoft-only Teams) that is only rendered once the connected
    # workspace matches; it is omitted from the provider-agnostic catalog.
    providers: tuple[str, ...] = ()
    # Workspace feature (scope bundle) this step needs the user to have
    # granted. ``None`` means the step is always eligible; a value (e.g.
    # ``"calendar"``) hides the step until that feature's scopes appear in
    # the connected workspace's granted-scopes secret. Feature names mirror
    # the bundles in ``assistant.scopes`` (email, calendar, drive, ...).
    requires_feature: str | None = None


@dataclass(frozen=True)
class OnboardingChip:
    """A read-only 'try one of these' suggestion shown under a step row.

    ``id`` is a stable key for the UI; ``label`` is the user-facing copy.
    """

    id: str
    label: str
    metadata: dict[str, Any] | None = None


# Transcript medium for Unify's org-owned Microsoft Teams bot. Used by the
# reply-first Teams onboarding derivation (the bot is reply-only: it cannot
# open a Teams conversation, so its onboarding steps derive from the user's
# own inbound message and Twin's reply into that conversation, never from a
# proactive outbound).
MS_TEAMS_BOT_MEDIUM = "ms_teams_bot_message"

# Proactive reference-quiz channels: Twin sends the clue first (the row is a
# ``_trigger``). Microsoft Teams is deliberately absent — its bot is reply-only,
# so its clue exchange is modelled reply-first (see the ms-teams steps below).
REFERENCE_QUIZ_TOOL_BY_CHANNEL = {
    "email": "send_email",
    "whatsapp_message": "send_whatsapp",
    "whatsapp_call": "make_whatsapp_call_to_boss",
    "sms_message": "send_sms",
    "phone_call": "make_call_to_boss",
    "slack_message": "send_slack_message",
    "discord_message": "send_discord_message",
}

REFERENCE_QUIZ_CHANNEL_BY_REPLY_STEP = {
    "email-reply": "email",
    "whatsapp-message": "whatsapp_message",
    "whatsapp-call": "whatsapp_call",
    "sms-message": "sms_message",
    "phone-call": "phone_call",
    "slack-message": "slack_message",
    "discord-message": "discord_message",
}


# Phase labels, in display order.
PHASE_COMMUNICATION = "Communication"
PHASE_WORKSPACE = "Workspace"
PHASE_INTEGRATIONS = "Integrations"
PHASE_TASKS = "Tasks"
PHASE_LEARNING = "Learning"
PHASE_CANVAS = "Canvas"
PHASE_MY_COMPUTER = "Your Computer"
PHASE_YOUR_COMPUTER = "Their Computer"
PHASE_TEAMS = "Teams"
PHASE_HIRING = "Hiring"

COMMUNICATION_FRAMING = (
    "Start onboarding by proving that T-W1N can communicate with the user "
    "across channels. Frame this as a light science-fiction reference quiz — "
    "strictly sci-fi (e.g. Star Wars, Star Trek, Dune, Blade Runner, The Matrix, "
    "Firefly, 2001, Hitchhiker's Guide), NEVER general trivia or fantasy such as "
    "Lord of the Rings, Harry Potter, or Game of Thrones. There is no fixed list "
    "of clues: on each channel T-W1N invents its "
    "own short sci-fi quote clue on the spot — a fresh, different sci-fi "
    "reference each time, "
    "T-W1N's own creative choice — sends it on that channel, the user guesses "
    "the reference there, T-W1N supports repeats and gentle hints, reveals the "
    "answer if asked or if the user is stuck, then closes naturally before "
    "moving on. Because the whole point of this phase is to prove the channels "
    "work in both directions, when the user replies on a channel (email, SMS, or "
    "WhatsApp), T-W1N replies back briefly on that same channel to confirm their "
    "guess — a deliberate exception to the usual rule of not sending texts "
    "during a call. If a call is also active, T-W1N still acknowledges verbally "
    "as normal, so the confirmation lands in both places."
)

WORKSPACE_FRAMING = (
    "Once the user has connected their Google or Microsoft workspace, T-W1N "
    "proves the connection is real and immediately useful by actually reading "
    "from it and reporting back — never by asking the user to do the work. For "
    "each workspace demo T-W1N reads the relevant area with its own tools "
    "(recent mailbox, Drive/OneDrive files, or the upcoming week's calendar), then "
    "delivers one short, plain-spoken summary to the user as a single "
    "unify_message — sent as an assistant message back to the user, not merely "
    "spoken on a call. Delivering that summary is the demo task; the checklist "
    "does NOT auto-detect it, so handling the demo is not finished until that "
    "summary deliverable has been sent to the user as a unify_message. "
    "Afterwards T-W1N offers exactly one natural follow-up and only acts on it if "
    "the user says yes: draft a reply to a notable email, suggest a simple "
    "optional way to tidy a messy Drive, or flag a conflict or gap on the "
    "calendar — this follow-up is optional and never gates completion. If the "
    "area is empty or T-W1N genuinely cannot read it, it says so honestly, still "
    "marks the step done, and moves on rather than inventing content."
)

INTEGRATIONS_FRAMING = (
    "In the Integrations phase T-W1N proves connected apps are more than a "
    "gallery: first the user connects at least one non-workspace app, then "
    "T-W1N reads from a connected app and finally takes one concrete, "
    "user-safe action across connected apps. For each live demo T-W1N chooses "
    "a connected app that fits the user's click or chip, explains any missing "
    "connection plainly, and never pretends an app is connected. The checklist "
    "does NOT auto-detect read/action demos, so handling each demo is not "
    "finished until the demo result has been sent to the user as a unify_message."
)


@dataclass(frozen=True)
class DemoContract:
    """Shared semantics for demo rows that complete by explicit brain action."""

    phase: str
    phase_id: str
    framing: str
    interaction_type: str
    subtype: str
    domain: str
    instruction: str


WORKSPACE_DEMO_CONTRACT = DemoContract(
    phase=PHASE_WORKSPACE,
    phase_id="workspace",
    framing=WORKSPACE_FRAMING,
    interaction_type="workspace_demo",
    subtype="workspace_demo_requested",
    domain="workspace",
    instruction=(
        "read the relevant part of their connected workspace with its own tools "
        "and deliver one short summary as a single unify_message"
    ),
)

INTEGRATION_READ_DEMO_CONTRACT = DemoContract(
    phase=PHASE_INTEGRATIONS,
    phase_id="integrations",
    framing=INTEGRATIONS_FRAMING,
    interaction_type="integration_demo",
    subtype="integration_demo_requested",
    domain="integration",
    instruction=(
        "read from one connected app that fits their request and deliver one "
        "short brief as a single unify_message"
    ),
)

INTEGRATION_ACTION_DEMO_CONTRACT = DemoContract(
    phase=PHASE_INTEGRATIONS,
    phase_id="integrations",
    framing=INTEGRATIONS_FRAMING,
    interaction_type="integration_demo",
    subtype="integration_demo_requested",
    domain="integration",
    instruction=(
        "take one concrete, user-safe action in a connected app or across "
        "connected apps, then report exactly what happened as a single "
        "unify_message"
    ),
)

TASKS_FRAMING = (
    "In the Tasks phase T-W1N proves it works on the user's behalf when they "
    "aren't watching, by setting up real standing work. A 'scheduled task' is "
    "a time-based task that fires on a schedule — a one-off soon or a recurring "
    "routine — and reaches back out on a channel the user has connected. A "
    "'triggerable task' is an event-triggered task that fires when something "
    "happens in the user's world (an urgent email lands, an after-hours message "
    "arrives, a calendar invite shows up). T-W1N sets these up from a "
    "plain-language "
    "description with its own task tools, asking only for details it genuinely "
    "needs (what to do, when or on what event, and which channel to reach the "
    "user on) and filling in sensible defaults otherwise. When it doesn't yet "
    "know what the user wants it asks in one short message and offers a couple "
    "of concrete examples; it never nags."
)

# Console surfaces T-W1N points the user at during the Tasks phase, so it
# teaches the UI alongside the capability. The names match the right-pane tabs
# the user actually sees (mirrored in unity ``console_ui.RIGHT_PANE_TABS``): the
# 'Tasks' tab lists the task definition once created, and the 'Actions' tab is
# the live feed of a run in progress.
_TASKS_TAB_NUDGE = "point them to the Tasks tab, where the new task now shows up"
_ACTIONS_TAB_NUDGE = (
    "tell the user to open the Actions tab themselves so they can watch my work "
    "live while a run is in progress — I have no tool to navigate the Console "
    "for them"
)

# Brain rail sections T-W1N tells the user to open after storing learning.
_BRAIN_GUIDANCE_NUDGE = (
    "tell the user to open the Guidance section in the Brain rail themselves, "
    "where the new rules now live — I have no tool to navigate the Console "
    "for them"
)
_BRAIN_FUNCTIONS_NUDGE = (
    "tell the user to open the Functions section in the Brain rail themselves, "
    "where the reusable procedure now lives — I have no tool to navigate the "
    "Console for them"
)

# The Learning beat is one openly-narrated tutorial (scripted narrative, real
# mechanics) over the seeded Expenses ETL example. One constant so the phase
# framing and the beat event tell the same story.
_LEARNING_ARC_PREVIEW = "; ".join(learning_expenses_intro_arc_lines())
LEARNING_FRAMING = (
    "The Learning phase is an openly narrated tutorial over seeded bank exports. "
    f"{learning_expenses_concepts_opening_guidance()} "
    "The hands-on demo shows this in action: one user correction becomes durable "
    "Guidance (playbook) and a reusable Function (skill); replay on fresh data "
    "proves it stuck. "
    f"{learning_expenses_contrivance_acknowledgment()} "
    "Before any attachments, preview the full hands-on arc up front: "
    f"{_LEARNING_ARC_PREVIEW}. "
    "Rule 1 — "
    f"{learning_expenses_deliverable_handoff_rule()} "
    "Rule 2 — Opening voice: "
    f"{learning_expenses_opening_script_guidance()} "
    "Rule 2b — User-facing deliverables: "
    f"{learning_expenses_user_facing_voice()} "
    "Rule 3 — Attachments: before the first attempt, send the month-N bank "
    "export CSVs as unify_message attachments (one attachment per message). "
    f"{learning_expenses_checking_attachment_description()} "
    f"{learning_expenses_card_attachment_description()} "
    "Rule 4 — First act: run a deliberately naive first pass over the month-N "
    "files via act(persist=True) — "
    f"{LEARNING_EXPENSES_NAIVE_MISTAKE_DESCRIPTION} "
    "from the fixtures; numbers are genuinely computed, never asserted. "
    "Rule 5 — After the first act completes, send the naive result as a "
    "unify_message (see Rule 1). State the naive total and explain the mistake "
    "in plain language (Rule 2b) — never forward act tables or row-by-row math. "
    "Suggest this exact correction text "
    f'for the user to send: "{LEARNING_EXPENSES_USER_CORRECTION_TEXT}" — '
    "then WAIT; never send the correction or proceed on their behalf. "
    "Rule 6 — After their correction: interject into the running persist act "
    "with the corrected algorithm and include this StorageCheck memoization "
    f"request verbatim: {learning_expenses_storage_check_nudge()} "
    "Send the improved deliverable as a unify_message. The doing loop must not "
    "call store tools — StorageCheck persists after the act completes; after "
    f"StorageCheck finishes, cite the stored ids from its summary when nudging "
    f"the user, then {_BRAIN_GUIDANCE_NUDGE} and {_BRAIN_FUNCTIONS_NUDGE}. "
    f"Rule 6b — {learning_expenses_stop_act_for_storage_rule()} "
    "Rule 7 — Invite them to ask for next month's report and WAIT; replay only "
    f"once they ask ({LEARNING_EXPENSES_REPLAY_HINT}). "
    "Rule 8 — Replay: second act(persist=True) over month-N+1 files; send the "
    "replay deliverable as a unify_message. Brain nudges and attachment intro "
    "messages are not deliverables. "
    f"Before and during each act run, {_ACTIONS_TAB_NUDGE}. "
    "Rule 9 — After sending the replay deliverable, the tutorial deliverable "
    "contract is complete — the checklist does not auto-detect the tutorial."
)

# Interaction channel id stamped on the Learning beat event (Unity narration).
LEARNING_BEAT_CHANNEL = "learning_beat"

# The My Computer beat is a call-anchored live desktop demo on T-W1N's managed
# VM. One constant so the phase framing and the beat event tell the same story.
MY_COMPUTER_FRAMING = (
    "The My Computer phase shows T-W1N has a real computer of its own — the user "
    "just watched it use it on a call. "
    "Rule 1 — Off-call click: call prepare_desktop immediately in the same turn as "
    "ONE short ack (tutorial intro: this step shows I have a real computer of my "
    "own; you'll watch me use it live — plus that I'm getting it ready). Do NOT "
    "ring and do NOT call start_unify_meet in that click turn — the slow brain is "
    "single-shot and cannot branch on prepare_desktop's return value there. "
    "When the desktop-ready notification lands (it always does for both already-"
    "ready and still-warming desktops), send ONE short chat line that the computer "
    "is ready and you are ringing now, then call start_unify_meet in that same "
    "turn. "
    "Opener = tutorial-esque intro spoken naturally (what they'll watch, questions "
    "welcome anytime); briefing = the full demo script below. Unanswered ring: ONE "
    "chat line inviting a ring-back; do not re-ring. "
    "Rule 2 — On-call click: call prepare_desktop if needed, then start the demo "
    "when the desktop-ready notification lands (or immediately if the desktop is "
    "already usable on that turn) and tell them to click Show assistant screen so "
    "they watch live. "
    "Rule 3 — Narrated persist-act demo: launch act(persist=True) during the ring "
    "(the actor's setup pass overlaps the user answering). Drive it substep by "
    "substep: send one substep, and when the act responds, narrate ONE short line on "
    "voice via guide_voice_agent and interject the next substep. Never batch the "
    "whole demo into one act request. Fixed substeps: (1) verify the desktop session "
    "and take an orienting screenshot; (2) open a visible browser and navigate to "
    "NASA's Astronomy Picture of the Day; (3) save today's image with the browser GUI "
    "only — right-click the main image → Save Image As… → confirm Save in the dialog "
    "(keep the default filename; leave it in the dialog's default folder, normally "
    "/Unity/Downloads — do not create a subfolder). Never urllib, curl, wget, "
    "Python HTTP download, or any other headless/programmatic save; (4) close the "
    "browser window, open Thunar (the GUI file manager) from the dock, and navigate "
    "to the folder used in the Save dialog (default /Unity/Downloads) so the saved "
    "file is visibly listed — never a terminal, never shell commands like xdg-open; "
    "(5) in Thunar, right-click the saved image → Open With → Ristretto (the image "
    "viewer) so the user sees the picture open on the desktop; (6) the actor sends "
    "the attachment itself: instruct it to run execute_code with "
    "await primitives.comms.send_unify_message(content=<one short caption>, "
    "attachment_filepath=<the exact saved path from the Save dialog>), then respond "
    "confirming delivery and the exact path it sent. If a substep is dragging "
    "(~2 minutes), simplify it or move on honestly — never grind silently. If the "
    "user asks for something else mid-call, honor it as long as it keeps the same "
    "shape (real site → GUI Save Image As → Thunar reveal → Ristretto open → "
    "deliver). "
    "Rule 4 — Tutorial voice throughout: plain language, no tool names; explain "
    "what they're seeing as it happens; invite questions mid-demo and answer them "
    "(the persist act pauses naturally between substeps). "
    "Rule 5 — Explicit completion: the demo is not finished until the actor's "
    "response confirms the attachment was delivered. Marking the step done, stopping "
    "act, and that delivery confirmation are three separate moments — never batch "
    "them into one turn. The CM never sends the attachment itself. If the actor "
    "reports the send failed, that is Rule-7 territory: say so, retry or offer "
    "later, do not mark done. The checklist does not auto-detect anything. "
    "Rule 6 — Contextual wrap-up: after marking done, give a one-line recap of what "
    "they watched (real computer, Save Image As, Thunar, Ristretto, delivered to "
    "chat), name "
    "the next onboarding step from the live progress block, and offer both paths — "
    "continue on this call or 'I'll message you the next step' — then respect their "
    "choice (gated hang-up if they're done). "
    "Rule 7 — Honest failure: if the VM won't boot, the site is unreachable, or the "
    "act fails, say so plainly, offer to retry later, and do not mark the step done. "
    "Rule 8 — Scope: nothing touching the user's own machine (that is the separate "
    "Your Computer phase). One beat, one concept."
)


@dataclass(frozen=True)
class OnboardingPhase:
    """A checklist phase header — the grouping row shown above its steps.

    ``label`` is the value stamped on each step's ``phase`` field (and the
    short legend label in the progress bar); ``id`` is the stable header-row
    id consumers key off (Console test ids, Unity prose grouping).
    ``local_only`` hides the whole phase — header and every step in it — on
    hosted deployments, leaving it visible only on a local self-host install.
    """

    id: str
    label: str
    title: str
    description: str
    framing: str = ""
    local_only: bool = False


# Phase headers, in display order. The single source of truth for phase
# grouping copy and per-phase deployment visibility.
ONBOARDING_PHASES: tuple[OnboardingPhase, ...] = (
    OnboardingPhase(
        id="communication",
        label=PHASE_COMMUNICATION,
        title="Communication",
        description="Try the communication channels I can use with you.",
        framing=COMMUNICATION_FRAMING,
    ),
    OnboardingPhase(
        id="workspace",
        label=PHASE_WORKSPACE,
        title="Workspace",
        description="Give T-W1N access to your Google or Microsoft workspace.",
        framing=WORKSPACE_FRAMING,
    ),
    OnboardingPhase(
        id="integrations",
        label=PHASE_INTEGRATIONS,
        title="Integrations",
        description="Connect the apps and services I should work with.",
        framing=INTEGRATIONS_FRAMING,
    ),
    OnboardingPhase(
        id="tasks",
        label=PHASE_TASKS,
        title="Tasks",
        description="Set up recurring or event-triggered work.",
        framing=TASKS_FRAMING,
    ),
    OnboardingPhase(
        id="learning",
        label=PHASE_LEARNING,
        title="Learning",
        description="Correct me once — I'll remember how you want it done.",
        framing=LEARNING_FRAMING,
    ),
    OnboardingPhase(
        id="canvas",
        label=PHASE_CANVAS,
        title="Canvas",
        description="Use a shared visual workspace.",
    ),
    OnboardingPhase(
        id="your-computer",
        label=PHASE_YOUR_COMPUTER,
        title="Their Computer",
        description="Let me help on your computer.",
    ),
    OnboardingPhase(
        id="my-computer",
        label=PHASE_MY_COMPUTER,
        title="Your Computer",
        description="Ask me to operate from my computer.",
        framing=MY_COMPUTER_FRAMING,
    ),
    OnboardingPhase(
        id="teams",
        label=PHASE_TEAMS,
        title="Teams",
        description="Work with your teammates.",
    ),
    OnboardingPhase(
        id="hiring",
        label=PHASE_HIRING,
        title="Hiring",
        description="Hire and configure assistants.",
    ),
)


def _trigger(
    step_id: str,
    title: str,
    *,
    depends_on: dict[str, int],
    channel: str,
    paired_reply: str,
    nudge_chat: str,
    nudge_voice: str,
) -> OnboardingStep:
    event_channel = REFERENCE_QUIZ_CHANNEL_BY_REPLY_STEP.get(paired_reply, channel)
    tool_name = REFERENCE_QUIZ_TOOL_BY_CHANNEL.get(event_channel, "")
    interaction = {
        "type": "reference_quiz",
        "trigger_step_id": step_id,
        "reply_step_id": paired_reply,
        "channel": event_channel,
        "tool_name": tool_name,
        "instructions": COMMUNICATION_FRAMING,
    }
    # The event is a *poll*, not a command: clicking the row tells Twin the
    # user is now expecting the clue on this channel. Twin may already have
    # sent it of its own accord (e.g. the user also asked verbally on a call) —
    # in that case the click and the spoken ask are the same directive in two
    # forms, and Twin must not send a duplicate.
    event = OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}', so they're now expecting the "
            "reference-quiz clue on that channel and are checking whether it "
            "has been sent. This is a poll, not a request to send another one: "
            "if you have already sent the clue (for example because they asked "
            "you to on a call), treat this as confirmation and do NOT send a "
            "duplicate."
        ),
        subtype="reference_quiz_clue_requested",
        details={
            "game": "guess_the_reference",
            "trigger_step_id": step_id,
            "reply_step_id": paired_reply,
            "channel": event_channel,
            "tool_name": tool_name,
            "framing": COMMUNICATION_FRAMING,
            "phase": PHASE_COMMUNICATION,
            "phase_id": "communication",
            "phase_framing": COMMUNICATION_FRAMING,
            "interaction": interaction,
        },
    )
    return OnboardingStep(
        id=step_id,
        title=title,
        phase=PHASE_COMMUNICATION,
        kind="trigger",
        depends_on=depends_on,
        can_skip=True,
        derivable=False,
        channel=channel,
        paired_reply=paired_reply,
        nudge_chat=nudge_chat,
        nudge_voice=nudge_voice,
        event=event,
    )


def _demo(
    step_id: str,
    title: str,
    *,
    channel: str,
    depends_on: dict[str, int],
    nudge_chat: str,
    nudge_voice: str,
    providers: tuple[str, ...] = (),
    requires_feature: str | None = None,
    contract: DemoContract = WORKSPACE_DEMO_CONTRACT,
) -> OnboardingStep:
    """A demo trigger row that completes explicitly after Twin performs it.

    Structurally a trigger (clicking it asks Twin to act now) but, unlike the
    reference-quiz triggers, it has no paired reply and is not auto-derived from
    an outbound: Twin performs the whole demo task, then explicitly marks the
    step done via ``set_onboarding_task_state`` (see ``MANUAL_COMPLETION_STEP_IDS`` /
    ``manual_completion_block_reason``). The ``workspace_demo`` interaction type
    lets Unity narrate it differently from a quiz clue.
    """
    interaction = {
        "type": contract.interaction_type,
        "trigger_step_id": step_id,
        "channel": channel,
        "instructions": contract.framing,
    }
    event = OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}', so they want T-W1N to run this "
            f"{contract.domain} demo now: {contract.instruction}. "
            "The checklist does NOT auto-detect that "
            "deliverable, so handling the demo is not finished until it has been "
            "sent to the user as a unify_message. Any reply, tidy-up, or "
            "flag is an optional follow-up offered afterwards and never required "
            "to complete the step. This is a poll, not a request to repeat work "
            "already done: if the task is already finished, treat this as "
            "confirmation and do NOT redo it."
        ),
        subtype=contract.subtype,
        details={
            "trigger_step_id": step_id,
            "channel": channel,
            "framing": contract.framing,
            "phase": contract.phase,
            "phase_id": contract.phase_id,
            "phase_framing": contract.framing,
            "interaction": interaction,
        },
    )
    return OnboardingStep(
        id=step_id,
        title=title,
        phase=contract.phase,
        kind="trigger",
        depends_on=depends_on,
        can_skip=True,
        derivable=False,
        channel=channel,
        paired_reply=None,
        nudge_chat=nudge_chat,
        nudge_voice=nudge_voice,
        event=event,
        providers=providers,
        requires_feature=requires_feature,
    )


# Tasks-phase beats. Clicking a beat row asks Twin to open a freeform
# conversation for that kind of standing work; clicking one of the row's
# example chips asks Twin to set up that specific task straight away. Both
# travel as ``coordinator_onboarding_event`` payloads, mirroring the
# reference-quiz and workspace-demo triggers. ``create-scheduled-task`` is
# scheduled (time-based) work; ``create-triggerable-task`` is event-triggered
# work.
_TASK_BEAT_KIND: dict[str, str] = {
    "create-scheduled-task": "scheduled",
    "create-triggerable-task": "triggered",
}

# Learning-phase beat: one row, no chips. The row click starts the openly
# scripted expenses-etl tutorial directly (see LEARNING_FRAMING).
_LEARNING_SCENARIO_ID = LEARNING_EXPENSES_SCENARIO_ID
_LEARNING_REPLAY_HINT = LEARNING_EXPENSES_REPLAY_HINT


def _task_beat_event(step_id: str, title: str) -> OnboardingEventSpec:
    """Event fired when the user clicks a Tasks-phase beat row.

    The row is the *freeform* entry point: the click tells Twin the user wants
    standing work of this kind but hasn't said what yet, so Twin opens the
    conversation by asking — it must not invent and create a task on its own.
    Clicking one of the row's example chips is the concrete path and travels as
    a separate ``task_chip_requested`` event (see :func:`chip_event_for`).
    """
    task_kind = _TASK_BEAT_KIND[step_id]
    if task_kind == "triggered":
        ask = (
            "ask them in one short message what should trip it and what you "
            "should do when it fires (for example: an urgent email arrives, an "
            "after-hours Slack message lands, a calendar invite shows up)"
        )
        after = (
            "Once they tell you, arm the trigger with your task tools and "
            f"confirm it in one line. Then {_TASKS_TAB_NUDGE}, and mention they "
            "can trip it right away with the 'Test it' control under the row "
            f"and {_ACTIONS_TAB_NUDGE}."
        )
    else:
        ask = (
            "ask them in one short message what job you should run and when — a "
            "one-off soon or a recurring routine (for example: sweep the inbox "
            "in a couple of minutes, a daily calendar rundown, a weekly recap)"
        )
        after = (
            "Once they tell you, schedule it with your task tools on a channel "
            f"they've connected and confirm it in one line. Then {_TASKS_TAB_NUDGE}, "
            f"and — since it can fire soon — {_ACTIONS_TAB_NUDGE}."
        )
    interaction = {
        "type": "task_beat",
        "trigger_step_id": step_id,
        "task_kind": task_kind,
        "instructions": TASKS_FRAMING,
    }
    return OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}', so they want to set up standing "
            f"work of this kind but haven't said what yet — {ask}. Do NOT create "
            f"a task until they've told you what they want. {after} If this is "
            "already set up, treat the click as a nudge and just confirm rather "
            "than duplicating it."
        ),
        subtype="task_beat_requested",
        details={
            "trigger_step_id": step_id,
            "task_kind": task_kind,
            "framing": TASKS_FRAMING,
            "phase": PHASE_TASKS,
            "phase_id": "tasks",
            "phase_framing": TASKS_FRAMING,
            "interaction": interaction,
        },
    )


def _learning_beat_event(step_id: str, title: str) -> OnboardingEventSpec:
    """Event fired when the user clicks the Learning beat row.

    The click starts the guided expenses-etl tutorial directly — an openly
    narrated correction loop over seeded bank exports, scripted end to end by
    ``LEARNING_FRAMING``. There is no freeform mode and there are no chips.
    """
    interaction = {
        "type": "learning_beat",
        "trigger_step_id": step_id,
        "channel": LEARNING_BEAT_CHANNEL,
        "scenario_id": _LEARNING_SCENARIO_ID,
        "instructions": LEARNING_FRAMING,
    }
    return OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}' — run the guided learning demo now. "
            f"{learning_expenses_opening_script_guidance()} "
            "Then send the two January bank export CSVs as unify_message attachments "
            "(one file per message), describing each file's rows as you send it: "
            f"{learning_expenses_checking_attachment_description()} "
            f"{learning_expenses_card_attachment_description()} "
            f"Tell them to open the Actions tab before the first act. "
            f"Rule — {learning_expenses_deliverable_handoff_rule()} "
            "Run act(persist=True) for the naive first pass "
            f"({LEARNING_EXPENSES_NAIVE_MISTAKE_DESCRIPTION}; real computed "
            "numbers only). When that act completes, your SAME turn must send "
            "the result as a unify_message — never a bare wait. Surface the "
            "mistake, suggest this correction for them "
            f'to send: "{LEARNING_EXPENSES_USER_CORRECTION_TEXT}", then WAIT. '
            "After their correction: revise, store Guidance and Function, send "
            "the improved deliverable, "
            f"{learning_expenses_stop_act_for_storage_rule()} "
            f"then {_BRAIN_GUIDANCE_NUDGE} and {_BRAIN_FUNCTIONS_NUDGE}. "
            f"Invite them to ask for next month's report and WAIT. Replay: "
            f"{_LEARNING_REPLAY_HINT} Send the replay deliverable — the tutorial "
            "deliverable contract is then complete. "
            f"{_ACTIONS_TAB_NUDGE} before and during each act run. "
            f"Full contract: {LEARNING_FRAMING}"
        ),
        subtype="learning_beat_requested",
        details={
            "trigger_step_id": step_id,
            "channel": LEARNING_BEAT_CHANNEL,
            "scenario_id": _LEARNING_SCENARIO_ID,
            "replay_hint": _LEARNING_REPLAY_HINT,
            "framing": LEARNING_FRAMING,
            "phase": PHASE_LEARNING,
            "phase_id": "learning",
            "phase_framing": LEARNING_FRAMING,
            "interaction": interaction,
        },
    )


def _my_computer_beat_event(step_id: str, title: str) -> OnboardingEventSpec:
    """Event fired when the user clicks the My Computer beat row.

    The click starts the call-anchored live desktop demo — persist-act substeps
    on the managed VM (GUI Save Image As, Thunar reveal, Ristretto open, chat
    attachment) — scripted by ``MY_COMPUTER_FRAMING``. There is no freeform mode
    and there are no chips.
    """
    interaction = {
        "type": "my_computer_beat",
        "trigger_step_id": step_id,
        "instructions": MY_COMPUTER_FRAMING,
    }
    return OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}'. "
            "Off-call: call prepare_desktop immediately in the same turn as a short "
            "ack; do not ring in that click turn. When the desktop-ready notification "
            "lands, send one short 'ready, ringing now' chat line and call "
            "start_unify_meet in that same turn. Launch act(persist=True) during the "
            "ring. On-call: call prepare_desktop if needed; start the persist-act "
            "demo when the desktop-ready notification lands (or immediately if "
            "already usable) and tell them to click Show assistant screen. Drive six "
            "substeps one at a time — act "
            "response → one guide_voice_agent line → interject next substep: verify "
            "desktop and screenshot; browser to NASA APOD; GUI Save Image As… into "
            "the dialog default folder (normally /Unity/Downloads; keep default "
            "filename; no programmatic download); close browser, open Thunar, "
            "navigate to that folder; right-click → Open With → Ristretto; actor "
            "sends send_unify_message attachment via execute_code and confirms "
            "delivery plus the exact path. Never a terminal or shell xdg-open. After "
            "the actor confirms delivery, mark the step done in its own turn, then "
            "stop act, then wrap up from the live progress block (recap, name next "
            "step, continue on call or message). Unanswered ring: one ring-back "
            "invite; do not re-ring. On failure: say so, do not mark done. This is "
            "a poll, not a request to repeat work already done: if the demo is "
            "already finished, treat this as confirmation and do NOT redo it. "
            f"Full contract: {MY_COMPUTER_FRAMING}"
        ),
        subtype="my_computer_beat_requested",
        details={
            "trigger_step_id": step_id,
            "framing": MY_COMPUTER_FRAMING,
            "phase": PHASE_MY_COMPUTER,
            "phase_id": "my-computer",
            "phase_framing": MY_COMPUTER_FRAMING,
            "interaction": interaction,
        },
    )


def _coming_soon(step_id: str, phase: str) -> OnboardingStep:
    return OnboardingStep(
        id=step_id,
        title="[Coming soon]",
        phase=phase,
        kind="coming_soon",
        depends_on={},
        can_skip=False,
        derivable=False,
    )


# Ordered graph. Order is the default display / tie-break order; the real
# gating comes from ``depends_on``. Phase boundaries do not create dependencies:
# the first step in a phase starts independently unless an explicit product
# constraint belongs on that step.
ONBOARDING_GRAPH: tuple[OnboardingStep, ...] = (
    _trigger(
        "email-reference",
        "Trigger email from T-W1N",
        depends_on={},
        channel="email",
        paired_reply="email-reply",
        nudge_chat=(
            "Explain the communication-channel reference quiz, then invite them "
            "to click the 'Trigger email from T-W1N' row in the Onboarding "
            "checklist for the first clue."
        ),
        nudge_voice=(
            "starting the communication-channel reference quiz by clicking the "
            "'Trigger email from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="email-reply",
        title="Reply to email",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"email-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="email",
        nudge_chat="Prompt them to reply with their guess to the email clue you sent.",
        nudge_voice="replying with their guess for the email clue",
    ),
    OnboardingStep(
        id="whatsapp-number",
        title="Add your WhatsApp number",
        phase=PHASE_COMMUNICATION,
        kind="setup",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="whatsapp",
        nudge_chat=(
            "Have them click the 'Add your WhatsApp number' row in the "
            "Onboarding checklist; it opens Account → Contact info so they "
            "can add or verify the number."
        ),
        nudge_voice=(
            "clicking the 'Add your WhatsApp number' row in the Onboarding checklist"
        ),
    ),
    _trigger(
        "whatsapp-message-reference",
        "Trigger WhatsApp message from T-W1N",
        depends_on={"whatsapp-number": COMPLETED},
        channel="whatsapp",
        paired_reply="whatsapp-message",
        nudge_chat=(
            "Invite them to click the 'Trigger WhatsApp message from T-W1N' row "
            "in the Onboarding checklist to get a clue over WhatsApp."
        ),
        nudge_voice=(
            "clicking the 'Trigger WhatsApp message from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="whatsapp-message",
        title="Reply to WhatsApp message",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"whatsapp-message-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="whatsapp",
        nudge_chat="Prompt them to reply with their guess to the WhatsApp clue you sent.",
        nudge_voice="replying to the WhatsApp message",
    ),
    _trigger(
        "whatsapp-call-reference",
        "Trigger WhatsApp call from T-W1N",
        depends_on={"whatsapp-number": COMPLETED},
        channel="whatsapp",
        paired_reply="whatsapp-call",
        nudge_chat=(
            "Invite them to click the 'Trigger WhatsApp call from T-W1N' row in "
            "the Onboarding checklist. Explain that WhatsApp may ask them to "
            "allow calls from the business first; after they approve, I will "
            "place the actual WhatsApp call with the clue."
        ),
        nudge_voice=(
            "clicking the 'Trigger WhatsApp call from T-W1N' row in the Onboarding checklist, "
            "then approving the WhatsApp call permission prompt if it appears"
        ),
    ),
    OnboardingStep(
        id="whatsapp-call",
        title="Answer WhatsApp call",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"whatsapp-call-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="whatsapp",
        nudge_chat="On the WhatsApp call, give the clue and let them guess.",
        nudge_voice="answering the WhatsApp call",
    ),
    OnboardingStep(
        id="phone-number",
        title="Add your phone number",
        phase=PHASE_COMMUNICATION,
        kind="setup",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="phone",
        nudge_chat=(
            "Have them click the 'Add your phone number' row in the "
            "Onboarding checklist; it opens Account → Contact info so they "
            "can add or verify the number."
        ),
        nudge_voice=(
            "clicking the 'Add your phone number' row in the Onboarding checklist"
        ),
    ),
    _trigger(
        "sms-reference",
        "Trigger SMS message from T-W1N",
        depends_on={"phone-number": COMPLETED},
        channel="sms",
        paired_reply="sms-message",
        nudge_chat=(
            "Invite them to click the 'Trigger SMS message from T-W1N' row in "
            "the Onboarding checklist to get a clue over SMS."
        ),
        nudge_voice=(
            "clicking the 'Trigger SMS message from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="sms-message",
        title="Reply to SMS message",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"sms-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="sms",
        nudge_chat="Prompt them to reply with their guess to the SMS clue you sent.",
        nudge_voice="replying to the SMS message",
    ),
    _trigger(
        "phone-call-reference",
        "Trigger phone call from T-W1N",
        depends_on={"phone-number": COMPLETED},
        channel="phone",
        paired_reply="phone-call",
        nudge_chat=(
            "Invite them to click the 'Trigger phone call from T-W1N' row in the "
            "Onboarding checklist to get a clue over a phone call."
        ),
        nudge_voice=(
            "clicking the 'Trigger phone call from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="phone-call",
        title="Answer phone call",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"phone-call-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="phone",
        nudge_chat="On the phone call, give the clue and let them guess.",
        nudge_voice="answering the phone call",
    ),
    OnboardingStep(
        id="slack-connect",
        title="Connect Slack",
        phase=PHASE_COMMUNICATION,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="slack",
        nudge_chat=(
            "Have them click the 'Connect Slack' row in the Onboarding checklist; "
            "it opens the Slack setup path for the Unify app. Heads up that many "
            "workspaces need an admin to approve the app, so if they're not an "
            "admin the install can sit pending until an admin connects it."
        ),
        nudge_voice="clicking the 'Connect Slack' row in the Onboarding checklist",
    ),
    _trigger(
        "slack-reference",
        "Trigger Slack message from T-W1N",
        depends_on={"slack-connect": COMPLETED},
        channel="slack",
        paired_reply="slack-message",
        nudge_chat=(
            "Invite them to click the 'Trigger Slack message from T-W1N' row in "
            "the Onboarding checklist to get a clue in Slack."
        ),
        nudge_voice=(
            "clicking the 'Trigger Slack message from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="slack-message",
        title="Reply to Slack message",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"slack-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="slack",
        nudge_chat="Prompt them to reply with their guess to the Slack message you sent.",
        nudge_voice="replying to the Slack message",
    ),
    OnboardingStep(
        id="ms-teams-connect",
        title="Connect Microsoft Teams",
        phase=PHASE_COMMUNICATION,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="ms_teams",
        nudge_chat=(
            "Have them click the 'Connect Microsoft Teams' row in the "
            "Onboarding checklist; it opens the setup path for the Unify Teams "
            "app. Heads up that many Microsoft 365 tenants need an admin to "
            "approve the app, so if they're not an admin the install can sit "
            "pending until an admin connects it."
        ),
        nudge_voice=(
            "clicking the 'Connect Microsoft Teams' row in the Onboarding checklist"
        ),
    ),
    # The Unify Teams bot is reply-only: it cannot open a conversation, so
    # unlike every other reference-quiz channel Twin cannot send the first
    # clue. This step is therefore user-initiated — clicking it opens the
    # Teams chat (via a deep link in Console) so the user sends Twin a first
    # message, which seeds the conversation reference and lets Twin reply.
    # Completion derives from that inbound message, not a Twin outbound.
    OnboardingStep(
        id="ms-teams-reference",
        title="Send your first message to Twin on Teams",
        phase=PHASE_COMMUNICATION,
        kind="setup",
        depends_on={"ms-teams-connect": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="ms_teams",
        nudge_chat=(
            "Have them click the 'Send your first message to Twin on Teams' "
            "row in the Onboarding checklist; it opens the Teams chat with the "
            "Unify bot (adding it for them first if needed) so they can say a "
            "quick hello. That first message is what opens the channel so I can "
            "reply — the Teams bot can't message first."
        ),
        nudge_voice=(
            "clicking the 'Send your first message to Twin on Teams' row in the "
            "Onboarding checklist and saying hello"
        ),
    ),
    OnboardingStep(
        id="ms-teams-message",
        title="Wait for T-W1N's reply in Teams",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"ms-teams-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="ms_teams",
        nudge_chat=(
            "Once they've said hello on Teams, reply to them there so they "
            "see Twin answer inside Teams. This step completes on that reply."
        ),
        nudge_voice="replying to them in Microsoft Teams",
    ),
    OnboardingStep(
        id="discord-id",
        title="Add your Discord ID",
        phase=PHASE_COMMUNICATION,
        kind="setup",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="discord",
        nudge_chat=(
            "Have them click the 'Add your Discord ID' row in the Onboarding "
            "checklist; it opens Account → Contact info so they can copy their "
            "Discord user ID (Settings → Advanced → Developer Mode, then "
            "click their name → Copy User ID) and save it."
        ),
        nudge_voice=(
            "clicking the 'Add your Discord ID' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="discord-connect",
        title="Connect Discord",
        phase=PHASE_COMMUNICATION,
        kind="connect",
        depends_on={"discord-id": COMPLETED},
        can_skip=True,
        derivable=False,
        channel="discord",
        nudge_chat=(
            "Have them click the 'Connect Discord' row in the Onboarding checklist; "
            "it walks them through adding my public bot. Remind them the bot can "
            "only DM them once they share a server with it."
        ),
        nudge_voice="clicking the 'Connect Discord' row in the Onboarding checklist",
    ),
    _trigger(
        "discord-reference",
        "Trigger Discord message from T-W1N",
        depends_on={"discord-connect": COMPLETED},
        channel="discord",
        paired_reply="discord-message",
        nudge_chat=(
            "Invite them to click the 'Trigger Discord message from T-W1N' row in "
            "the Onboarding checklist to get a clue in Discord."
        ),
        nudge_voice=(
            "clicking the 'Trigger Discord message from T-W1N' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="discord-message",
        title="Reply to Discord message",
        phase=PHASE_COMMUNICATION,
        kind="reply",
        depends_on={"discord-reference": COMPLETED},
        can_skip=True,
        derivable=True,
        channel="discord",
        nudge_chat="Prompt them to reply with their guess to the Discord message you sent.",
        nudge_voice="replying to the Discord message",
    ),
    OnboardingStep(
        id="workspace",
        title="Give T-W1N access to your workspace",
        phase=PHASE_WORKSPACE,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Give T-W1N access to your workspace' row in the "
            "Onboarding checklist; it opens the Google or Microsoft workspace "
            "connection flow."
        ),
        nudge_voice=(
            "clicking the 'Give T-W1N access to your workspace' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-mailbox",
        "Ask T-W1N to summarize your mailbox",
        channel="workspace_mailbox",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Once their workspace is connected, invite them to click the "
            "'Ask T-W1N to summarize your mailbox' row in the Onboarding checklist; I read their "
            "recent mail and send back a short summary, then offer to draft a reply."
        ),
        nudge_voice=(
            "clicking the 'Ask T-W1N to summarize your mailbox' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-drive",
        "Ask T-W1N to summarize your files",
        channel="workspace_drive",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Invite them to click the 'Ask T-W1N to summarize your files' row in the "
            "Onboarding checklist; I scan their Drive or OneDrive and send back a "
            "short summary, then suggest a simple, optional tidy-up if it looks messy."
        ),
        nudge_voice=(
            "clicking the 'Ask T-W1N to summarize your files' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-calendar",
        "Check my upcoming calendar events within a week",
        channel="workspace_calendar",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Invite them to click the 'Check my upcoming calendar events within "
            "a week' row in the Onboarding checklist; I read their calendar for "
            "the next week and send back a short summary, flagging any conflicts "
            "or gaps."
        ),
        nudge_voice=(
            "clicking the 'Check my upcoming calendar events within a week' row "
            "in the Onboarding checklist"
        ),
        requires_feature="calendar",
    ),
    OnboardingStep(
        id="apps",
        title="Connect T-W1N with your apps",
        phase=PHASE_INTEGRATIONS,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Connect T-W1N with your apps' row in the "
            "Onboarding checklist; it opens Integrations so they can connect "
            "at least one app (Slack, Gmail, Notion, ...)."
        ),
        nudge_voice=(
            "clicking the 'Connect T-W1N with your apps' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "integration-read",
        "Ask T-W1N to read from your connected apps",
        channel="integration_read",
        depends_on={"apps": COMPLETED},
        nudge_chat=(
            "Once they have connected an app, invite them to click the "
            "'Ask T-W1N to read from your connected apps' row in the Onboarding "
            "checklist; I read from a connected app and brief them here."
        ),
        nudge_voice=(
            "clicking the 'Ask T-W1N to read from your connected apps' row in the Onboarding checklist"
        ),
        contract=INTEGRATION_READ_DEMO_CONTRACT,
    ),
    _demo(
        "integration-action",
        "Ask T-W1N to take action across your apps",
        channel="integration_action",
        depends_on={"integration-read": ADDRESSED},
        nudge_chat=(
            "Invite them to click the 'Ask T-W1N to take action across your apps' "
            "row in the Onboarding checklist; I take one concrete, safe action "
            "with connected apps and report back."
        ),
        nudge_voice=(
            "clicking the 'Ask T-W1N to take action across your apps' row in the Onboarding checklist"
        ),
        contract=INTEGRATION_ACTION_DEMO_CONTRACT,
    ),
    OnboardingStep(
        id="create-scheduled-task",
        title="Create a scheduled task",
        phase=PHASE_TASKS,
        kind="schedule",
        depends_on={},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Create a scheduled task' row in the "
            "Onboarding checklist; it starts a short back-and-forth where I "
            "schedule a task for them and report back on a channel they've "
            "connected. They can also click one of the example chips under the "
            "row to set that one up directly."
        ),
        nudge_voice=(
            "clicking the 'Create a scheduled task' row in the Onboarding checklist"
        ),
        event=_task_beat_event("create-scheduled-task", "Create a scheduled task"),
    ),
    OnboardingStep(
        id="create-triggerable-task",
        title="Create a triggerable task",
        phase=PHASE_TASKS,
        kind="schedule",
        depends_on={"create-scheduled-task": ADDRESSED},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Create a triggerable task' row in the "
            "Onboarding checklist; it starts a short back-and-forth where I arm "
            "a task that fires on an event, which they can then trip with the "
            "Test-it control. They can also click one of the example chips under "
            "the row to arm that one directly."
        ),
        nudge_voice=(
            "clicking the 'Create a triggerable task' row in the Onboarding checklist"
        ),
        event=_task_beat_event("create-triggerable-task", "Create a triggerable task"),
    ),
    OnboardingStep(
        id="learn-from-correction",
        title="Teach me by correcting me",
        phase=PHASE_LEARNING,
        kind="schedule",
        depends_on={},
        can_skip=True,
        derivable=False,
        nudge_chat=(
            "Have them click the 'Teach me by correcting me' row in the "
            "Onboarding checklist. It starts a guided tutorial where I make "
            "a deliberate mistake on seeded bank exports, they correct me, "
            "I store the learning in Brain, and they prove it by asking me "
            "to run next month's report."
        ),
        nudge_voice=(
            "clicking the 'Teach me by correcting me' row in the Onboarding checklist"
        ),
        event=_learning_beat_event(
            "learn-from-correction",
            "Teach me by correcting me",
        ),
    ),
    _coming_soon("canvas-coming-soon", PHASE_CANVAS),
    OnboardingStep(
        id="my-computer-demo",
        title="Watch me work on my computer",
        phase=PHASE_MY_COMPUTER,
        kind="trigger",
        depends_on={},
        can_skip=True,
        derivable=False,
        paired_reply=None,
        nudge_chat=(
            "Have them click the 'Watch me work on my computer' row in the "
            "Onboarding checklist on a call — I drive my managed desktop live, "
            "fetch a file from the web, show it landing in my filesystem, and "
            "send it to them in chat."
        ),
        nudge_voice=(
            "clicking the 'Watch me work on my computer' row in the Onboarding "
            "checklist"
        ),
        event=_my_computer_beat_event(
            "my-computer-demo",
            "Watch me work on my computer",
        ),
    ),
    _coming_soon("your-computer-coming-soon", PHASE_YOUR_COMPUTER),
    _coming_soon("teams-coming-soon", PHASE_TEAMS),
    _coming_soon("hiring-coming-soon", PHASE_HIRING),
)


STEP_BY_ID: dict[str, OnboardingStep] = {step.id: step for step in ONBOARDING_GRAPH}

# Trigger row id → its paired reply step id. Built from the graph so the
# pairing can't drift from the step definitions.
TRIGGER_TO_REPLY: dict[str, str] = {
    step.id: step.paired_reply
    for step in ONBOARDING_GRAPH
    if step.kind == "trigger" and step.paired_reply
}

REPLY_TO_TRIGGER: dict[str, str] = {
    reply_id: trigger_id for trigger_id, reply_id in TRIGGER_TO_REPLY.items()
}

_CHANNEL_TO_OUTBOUND_MEDIUMS: dict[str, tuple[str, ...]] = {
    "email": ("email",),
    "whatsapp_message": ("whatsapp_message",),
    "whatsapp_call": ("whatsapp_call",),
    "sms_message": ("sms_message",),
    "phone_call": ("phone_call",),
    "slack_message": ("slack_message", "slack_channel_message"),
    "discord_message": ("discord_message", "discord_channel_message"),
}

# Trigger row id -> transcript medium(s) that prove Twin sent the outbound.
# Completion is derived from durable assistant-authored transcript rows rather
# than from the user's click or the active paired reply pointer. The row must
# also carry matching onboarding metadata for the trigger id.
TRIGGER_TO_OUTBOUND_MEDIUMS: dict[str, tuple[str, ...]] = {
    trigger_id: _CHANNEL_TO_OUTBOUND_MEDIUMS[
        REFERENCE_QUIZ_CHANNEL_BY_REPLY_STEP[reply_id]
    ]
    for trigger_id, reply_id in TRIGGER_TO_REPLY.items()
}

# Workspace and Integrations demo trigger rows have no paired reply and are
# deliberately NOT auto-derived from an outbound: a single tagged summary/report
# must not complete a multi-part task. The assistant performs the full demo task
# end to end, then explicitly marks the step done via
# ``set_onboarding_task_state`` (permitted for these ids by
# ``manual_completion_block_reason``), which records the step in
# ``manually_completed_step_ids``. They are therefore absent from
# ``TRIGGER_TO_OUTBOUND_MEDIUMS`` and are never picked up by
# ``derive_onboarding_progress`` from transcript evidence.
DEMO_STEP_IDS: tuple[str, ...] = (
    "workspace-mailbox",
    "workspace-drive",
    "workspace-calendar",
    "integration-read",
    "integration-action",
)

# Steps Twin may mark done via ``set_onboarding_task_state`` / the
# ``onboarding_step_completion`` PATCH. Demo ids are trigger rows with no paired
# reply and no transcript derivation; discord-connect sits in Communication but
# has no inbound auto-derive signal; learn-from-correction is an
# explicitly-completed tutorial beat; my-computer-demo is completed after the
# managed-desktop proof finishes.
MANUAL_COMPLETION_STEP_IDS: tuple[str, ...] = (
    *DEMO_STEP_IDS,
    "discord-connect",
    "learn-from-correction",
    "my-computer-demo",
)

# Steps whose completion Orchestra derives from durable domain state.
DERIVABLE_STEP_IDS: tuple[str, ...] = tuple(
    step.id for step in ONBOARDING_GRAPH if step.derivable
)

# Phase header lookup by the label stamped on each step.
PHASE_BY_LABEL: dict[str, OnboardingPhase] = {
    phase.label: phase for phase in ONBOARDING_PHASES
}


@dataclass(frozen=True)
class StepPresentation:
    """Per-step presentation copy: the info-tooltip ``description`` and rough
    ``estimated_time`` shown in Console, plus the read-only suggestion chips
    rendered under rows that carry examples."""

    description: str = ""
    estimated_time: str = ""
    chips_chat: tuple[OnboardingChip, ...] = ()
    chips_call: tuple[OnboardingChip, ...] = ()


# Time-bound work that reaches back out on its own. The first chip is a
# short-fuse "boomerang" the user can watch land live during onboarding and
# would genuinely keep (real inbox triage, not a test ping); the rest are
# real recurring routines referencing workspace data they've connected.
_INTEGRATION_CONNECT_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip(
        "day-to-day-tools",
        "Connect the apps you already check every day",
        {
            "gallery_category": "productivity",
            "search_query": "productivity|calendar|docs",
        },
    ),
    OnboardingChip(
        "crm-sales",
        "Connect a CRM or sales tool — if you use one",
        {
            "gallery_category": "crm_sales",
            "search_query": "crm|sales|hubspot|pipedrive",
        },
    ),
    OnboardingChip(
        "dev-ops",
        "Connect a dev, HR, or ops tool — whatever fits",
        {"gallery_category": "dev_ops", "search_query": "github|linear|jira|hr|ops"},
    ),
)

_INTEGRATION_READ_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip(
        "crm-pipeline-summary",
        "Summarise what's open in your connected CRM or pipeline",
    ),
    OnboardingChip(
        "dev-tool-activity",
        "Show me recent activity from a connected dev tool",
    ),
    OnboardingChip(
        "connected-app-brief",
        "Pull the latest from one of my connected apps and brief me here",
    ),
)

_INTEGRATION_ACTION_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip(
        "take-concrete-action",
        "Take one concrete action in a connected app and report back to me",
    ),
    OnboardingChip(
        "draft-follow-up",
        "Draft a follow-up from a connected app and send it to my workspace",
    ),
    OnboardingChip(
        "cross-app-update",
        "Update something in one app using info from another",
    ),
)

_SCHEDULED_TASK_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip(
        "inbox-sweep-soon",
        "In two minutes, check my inbox and text me anything urgent",
    ),
    OnboardingChip(
        "morning-calendar",
        "Every morning at 8am, send me a calendar rundown",
    ),
    OnboardingChip(
        "weekly-recap",
        "Every Friday afternoon, recap my week and email it to me",
    ),
)

# Event-bound work that fires when something happens in the user's world.
# Each names a trigger on a channel they've connected and a concrete output;
# the user trips one deterministically with the Test-it control to see it fire.
_TRIGGERABLE_TASK_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip(
        "urgent-email",
        "When I get an email marked urgent, text me straight away",
    ),
    OnboardingChip(
        "after-hours-slack",
        "When someone messages me on Slack after 6pm, send me a WhatsApp",
    ),
    OnboardingChip(
        "calendar-invite",
        "When a calendar invite lands for tomorrow, give me a heads-up here",
    ),
)

# Presentation copy keyed by step id. Lives beside the graph so every
# consumer (Console checklist, Unity prose) reads the same descriptions,
# time estimates, and suggestion chips from one place.
STEP_PRESENTATION: dict[str, StepPresentation] = {
    "email-reference": StepPresentation(
        "T-W1N introduces the reference quiz and sends the first clue to your email.",
        "~10s",
    ),
    "email-reply": StepPresentation("Reply to T-W1N's email with your guess.", "~30s"),
    "whatsapp-number": StepPresentation(
        "Add the WhatsApp number T-W1N should use.",
        "~30s",
    ),
    "whatsapp-message-reference": StepPresentation(
        "T-W1N sends the next reference clue over WhatsApp.",
        "~10s",
    ),
    "whatsapp-message": StepPresentation(
        "Reply to T-W1N's WhatsApp message with your guess.",
        "~1 min",
    ),
    "whatsapp-call-reference": StepPresentation(
        "T-W1N requests WhatsApp call permission, then calls with the next clue.",
        "~30s",
    ),
    "whatsapp-call": StepPresentation(
        "Allow the WhatsApp call if prompted, then answer and guess the clue.",
        "~1 min",
    ),
    "phone-number": StepPresentation(
        "Add the phone number T-W1N should use for calls and SMS.",
        "~30s",
    ),
    "sms-reference": StepPresentation(
        "T-W1N sends the next reference clue over SMS.",
        "~10s",
    ),
    "sms-message": StepPresentation(
        "Reply to T-W1N's SMS message with your guess.",
        "~1 min",
    ),
    "phone-call-reference": StepPresentation(
        "T-W1N calls with the next reference clue.",
        "~10s",
    ),
    "phone-call": StepPresentation(
        "Answer T-W1N's phone call and guess the clue.",
        "~1 min",
    ),
    "slack-connect": StepPresentation(
        "Install the Unify Slack app to your workspace so T-W1N can message "
        "you there.",
        "~1 min",
    ),
    "slack-reference": StepPresentation(
        "T-W1N sends the next reference clue in Slack.",
        "~10s",
    ),
    "slack-message": StepPresentation(
        "Reply to T-W1N's Slack message with your guess.",
        "~1 min",
    ),
    "ms-teams-connect": StepPresentation(
        "Install the Unify Microsoft Teams app to your organization so T-W1N "
        "can message you there.",
        "~1 min",
    ),
    "ms-teams-reference": StepPresentation(
        "Open Teams and send Twin a quick hello. The Unify bot can only reply, "
        "so your first message is what opens the channel.",
        "~1 min",
    ),
    "ms-teams-message": StepPresentation(
        "T-W1N replies to your hello inside Microsoft Teams.",
        "~1 min",
    ),
    "discord-id": StepPresentation(
        "Add your Discord user ID so T-W1N can DM you.",
        "~1 min",
    ),
    "discord-connect": StepPresentation(
        "Add T-W1N's public Discord bot so it can DM you.",
        "~1 min",
    ),
    "discord-reference": StepPresentation(
        "T-W1N sends the next reference clue in Discord.",
        "~10s",
    ),
    "discord-message": StepPresentation(
        "Reply to T-W1N's Discord message with your guess.",
        "~1 min",
    ),
    "workspace": StepPresentation(
        "Required for everything else in onboarding.",
        "~30s",
    ),
    "workspace-mailbox": StepPresentation(
        "T-W1N reads your recent mail and sends back a short summary, then "
        "offers to draft a reply.",
        "~30s",
    ),
    "workspace-drive": StepPresentation(
        "T-W1N scans your Drive or OneDrive and sends back a short summary, with "
        "an optional tidy-up suggestion if it looks messy.",
        "~30s",
    ),
    "workspace-calendar": StepPresentation(
        "T-W1N reviews your calendar for the next week and sends back a short "
        "summary, flagging any conflicts or gaps.",
        "~30s",
    ),
    "apps": StepPresentation(
        "Hook up at least one app from the Integrations gallery.",
        "~2 min",
        _INTEGRATION_CONNECT_CHIPS,
        _INTEGRATION_CONNECT_CHIPS,
    ),
    "integration-read": StepPresentation(
        "T-W1N reads from a connected app and briefs you here.",
        "~30s",
        _INTEGRATION_READ_CHIPS,
        _INTEGRATION_READ_CHIPS,
    ),
    "integration-action": StepPresentation(
        "T-W1N takes one concrete action with connected apps and reports back.",
        "~1 min",
        _INTEGRATION_ACTION_CHIPS,
        _INTEGRATION_ACTION_CHIPS,
    ),
    "create-scheduled-task": StepPresentation(
        "Schedule a task and watch me report back on your channel.",
        "~2 min",
        _SCHEDULED_TASK_CHIPS,
        _SCHEDULED_TASK_CHIPS,
    ),
    "create-triggerable-task": StepPresentation(
        "Set a task that fires on an event, then test it live.",
        "~2 min",
        _TRIGGERABLE_TASK_CHIPS,
        _TRIGGERABLE_TASK_CHIPS,
    ),
    "learn-from-correction": StepPresentation(
        "A guided demo: correct my first attempt and I'll store how you "
        "want it done, then prove it on the next one.",
        "~5 min",
    ),
    "my-computer-demo": StepPresentation(
        "T-W1N drives its own computer live on a call — it saves a file from the "
        "web with Save Image As, shows it in Thunar, opens it in Ristretto, and "
        "sends it to you here.",
        "~3 min",
    ),
}

_EMPTY_PRESENTATION = StepPresentation()

STEP_FLOW_NOTES: dict[str, str] = {
    "email-reference": (
        "Clicking the 'Trigger email from T-W1N' row tells me the user is ready "
        "for the reference quiz clue over email; if I haven't sent it yet I "
        "introduce the quiz and send my own clue, and if I already have I just "
        "confirm it's on the way rather than sending another."
    ),
    "email-reply": "The user replies with their guess once they receive the email clue.",
    "whatsapp-number": (
        "Clicking the 'Add your WhatsApp number' row opens Account -> Contact "
        "info so the user can add or verify the WhatsApp number."
    ),
    "whatsapp-message-reference": (
        "Clicking the 'Trigger WhatsApp message from T-W1N' row tells me the "
        "user is ready for the clue over WhatsApp; I send my own clue if I "
        "haven't already, otherwise I just confirm it."
    ),
    "whatsapp-message": "The user guesses the WhatsApp clue.",
    "whatsapp-call-reference": (
        "Clicking the 'Trigger WhatsApp call from T-W1N' row tells me the user "
        "is ready for a WhatsApp voice clue. WhatsApp Business Calling may first "
        "require them to approve calls from the business; I should say this up "
        "front, send/request the call, then wait for approval before the actual "
        "call is placed."
    ),
    "whatsapp-call": (
        "The user approves the WhatsApp call prompt if needed, answers the "
        "WhatsApp voice call, and guesses during the exchange."
    ),
    "phone-number": (
        "Clicking the 'Add your phone number' row opens Account -> Contact info "
        "so the user can add or verify the phone number."
    ),
    "sms-reference": (
        "Clicking the 'Trigger SMS message from T-W1N' row tells me the user is "
        "ready for the clue by text; I send my own clue if I haven't already, "
        "otherwise I just confirm it."
    ),
    "sms-message": "The user guesses the SMS clue.",
    "phone-call-reference": (
        "Clicking the 'Trigger phone call from T-W1N' row tells me the user is "
        "ready for a phone-call clue; I start or request the call unless I have "
        "already done so."
    ),
    "phone-call": "The user guesses during the phone call.",
    "slack-connect": (
        "Clicking the 'Connect Slack' row opens the Slack setup path for the "
        "Unify Slack app. Walk them through installing it to their workspace and "
        "choosing where I should reach them. The usual snag is workspace "
        "permissions: many Slack workspaces require an admin to approve new apps, "
        "so if they aren't an admin the install can sit in a 'pending approval' "
        "state and the row won't complete until the app is actually connected. "
        "Say that plainly, and if they can't approve it themselves the cleanest "
        "path is to have a workspace owner or admin do the connect."
    ),
    "slack-reference": (
        "Clicking the 'Trigger Slack message from T-W1N' row tells me the user "
        "is ready for the clue in Slack; I send my own clue if I haven't "
        "already, otherwise I just confirm it."
    ),
    "slack-message": "The user guesses the Slack clue.",
    "ms-teams-connect": (
        "Clicking the 'Connect Microsoft Teams' row opens the setup path for "
        "the Unify Teams app. Walk them through installing it to their "
        "organization and choosing where I should reach them. The usual snag "
        "is tenant permissions: many Microsoft 365 tenants require an admin to "
        "approve new apps, so if they aren't an admin the install can sit in a "
        "'pending approval' state and the row won't complete until the app is "
        "actually connected. Say that plainly, and if they can't approve it "
        "themselves the cleanest path is to have a tenant admin do the connect."
    ),
    "ms-teams-reference": (
        "Clicking the 'Send your first message to Twin on Teams' row opens the "
        "Teams chat with the Unify bot (adding it for the user first if "
        "needed). The Unify Teams bot is reply-only — it can't send the first "
        "message — so the user has to say hello there before I can do anything. "
        "This step completes when their first Teams message arrives; never "
        "claim to have messaged them on Teams before that, and never fake it "
        "with an api or unify message stand-in."
    ),
    "ms-teams-message": (
        "The Unify Teams bot is reply-only, so once the user's first Teams "
        "message has opened the channel I reply to them inside Teams. This "
        "step completes on that reply landing on Teams; never claim to have "
        "replied before the reply actually goes out, and never fake it with "
        "an api or unify message stand-in."
    ),
    "discord-id": (
        "Clicking the 'Add your Discord ID' row opens Account -> Contact info. "
        "Walk them through it: in Discord, turn on Settings -> Developer -> "
        "Developer Mode, then click their own name or profile photo, and 'Copy User ID' and "
        "paste that into the Discord ID field, then save."
    ),
    "discord-connect": (
        "Clicking the 'Connect Discord' row opens the Discord setup path so they "
        "can add T-W1N's public Discord bot from the link in the dialog. The "
        "thing that trips people up: the bot can only DM them once they share a "
        "server with it, so if my first Discord message never arrives that is "
        "almost always why -- have them add the bot to a server they're in and "
        "try the clue again."
    ),
    "discord-reference": (
        "Clicking the 'Trigger Discord message from T-W1N' row tells me the "
        "user is ready for the clue in Discord; I send my own clue if I haven't "
        "already, otherwise I just confirm it."
    ),
    "discord-message": "The user guesses the Discord clue.",
    "workspace": (
        "Clicking the 'Give T-W1N access to your workspace' row opens the workspace "
        "OAuth dialog (Google Workspace or Microsoft 365). Completing OAuth "
        "grants me access to their email, calendar, files, and other workspace "
        "resources."
    ),
    "workspace-mailbox": (
        "Clicking the 'Ask T-W1N to summarize your mailbox' row tells me the user wants a live "
        "demo of their connected mailbox: I read their recent mail with my own "
        "tools and deliver one short summary as a single unify_message. The "
        "checklist does not auto-detect that summary, so handling the demo is not "
        "finished until that summary deliverable has been sent. Offering or drafting "
        "a reply to a notable thread is an optional follow-up I only act on if "
        "the user says yes; it never gates completion. If I have already finished "
        "the task I just confirm it rather than redoing the work."
    ),
    "workspace-drive": (
        "Clicking the 'Ask T-W1N to summarize your files' row tells me the user wants a "
        "demo of their connected Drive or OneDrive: I read what's there and send "
        "one short summary as a single unify_message, then offer a simple, "
        "optional way to tidy things up if the files look disorganised (I only "
        "reorganise if they say yes). Once the summary deliverable has been sent, "
        "the demo task is genuinely done — the step does not auto-complete from "
        "the summary alone."
    ),
    "workspace-calendar": (
        "Clicking the 'Check my upcoming calendar events within a week' row "
        "tells me the user wants a demo of their connected calendar: I read "
        "their events for the next week and send one short summary as a single "
        "unify_message, flagging any conflicts or gaps. Once that summary "
        "deliverable has been sent, the demo task is done — the step does not "
        "auto-complete from the summary alone."
    ),
    "apps": (
        "Clicking the 'Connect T-W1N with your apps' row opens the Integrations "
        "tab; they connect at least one app from the gallery and authorize it."
    ),
    "integration-read": (
        "Clicking the 'Ask T-W1N to read from your connected apps' row tells me "
        "the user wants a live demo with connected apps: I read from an app that "
        "fits their request, send one short brief as a single unify_message. "
        "Handling the demo is not finished until that brief has been sent. "
        "If no connected app fits, I say exactly what is missing and leave "
        "the step pending."
    ),
    "integration-action": (
        "Clicking the 'Ask T-W1N to take action across your apps' row tells me "
        "the user wants a live action demo: I take one concrete, user-safe action "
        "with connected apps, send one short report as a single unify_message. "
        "Handling the demo is not finished until that report has been sent. "
        "If no connected app fits, I say exactly what is missing and leave "
        "what is missing and leave the step pending."
    ),
    "create-scheduled-task": (
        "Clicking the 'Create a scheduled task' row asks me to open the "
        "conversation: I ask what scheduled job they'd like and, once they tell "
        "me, schedule it with my task tools so it fires on its own and reports "
        "back on a channel they've connected — the proof is me returning "
        "unprompted, not the row existing. Once it's set up I point them to the "
        "Tasks tab to see it listed and to the Actions tab to watch it run live "
        "when it fires. The suggestion chips under the row are clickable: "
        "clicking one asks me to set up that specific task straight away."
    ),
    "create-triggerable-task": (
        "Clicking the 'Create a triggerable task' row asks me to open the "
        "conversation: I ask what event should trip it and, once they tell me, "
        "arm the event-triggered task with my task tools. They can then trip it "
        "deterministically with the Test-it control and watch it run; the "
        "trigger stays armed for the real event afterwards. Once it's armed I "
        "point them to the Tasks tab to see it listed and to the Actions tab to "
        "watch it run live when they trip it. The suggestion chips under the row "
        "are clickable: clicking one asks me to arm that specific triggerable "
        "task straight away."
    ),
    "learn-from-correction": (
        "Clicking the 'Teach me by correcting me' row starts an openly "
        "narrated tutorial: I first explain what learning, Guidance, and "
        "Functions are and why they matter, then walk through a seeded demo — "
        "month-N bank exports as chat attachments, a deliberately naive pass, "
        "my mistake, a correction for the user to send, and a wait. "
        "After they send it I revise, stop the persist act so StorageCheck can "
        "save the learning in Brain (Guidance and Functions), and invite them "
        "to ask me for next month's report — the replay runs only when they ask. "
        "When the replay deliverable is sent, I mark the step done explicitly — "
        "the checklist does not auto-detect the tutorial."
    ),
    "my-computer-demo": (
        "Clicking the 'Watch me work on my computer' row starts the live desktop "
        "demo on a call — T-W1N opens its browser on the managed VM, saves today's "
        "NASA Astronomy Picture of the Day via Save Image As, shows the file in "
        "Thunar under /Unity/Downloads, opens it in Ristretto, and sends it as a "
        "chat attachment; off-call the click is a call invitation instead. When "
        "the attachment is delivered, I mark the step done explicitly — nothing "
        "auto-completes."
    ),
}


def presentation_for(step_id: str) -> StepPresentation:
    """Presentation copy for a step (empty when none is registered)."""
    return STEP_PRESENTATION.get(step_id, _EMPTY_PRESENTATION)


# Per-provider description overrides applied at render time once the connected
# workspace provider is known. Only steps whose wording genuinely diverges by
# provider need an entry; the neutral ``STEP_PRESENTATION`` copy is the
# fallback for an unknown provider (and for the provider-agnostic catalog).
PROVIDER_PRESENTATION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "workspace-drive": {
        "google": (
            "T-W1N scans your Google Drive and sends back a short summary, with "
            "an optional tidy-up suggestion if it looks messy."
        ),
        "microsoft": (
            "T-W1N scans your OneDrive and SharePoint files and sends back a "
            "short summary, with an optional tidy-up suggestion if it looks messy."
        ),
    },
}


def presentation_description_for(step_id: str, *, provider: str | None = None) -> str:
    """Presentation description, specialised to the connected provider.

    Falls back to the neutral ``STEP_PRESENTATION`` copy when the provider is
    unknown or the step has no provider-specific variant.
    """
    variants = PROVIDER_PRESENTATION_DESCRIPTIONS.get(step_id)
    if provider and variants and provider in variants:
        return variants[provider]
    return presentation_for(step_id).description


def step_visible_for_provider(
    step: OnboardingStep,
    provider: str | None,
) -> bool:
    """Whether a step renders for the connected workspace provider.

    Provider-agnostic steps (``providers == ()``) always render. A
    provider-exclusive step (e.g. Microsoft-only Teams) renders only when the
    connected workspace matches; with no connected provider it stays hidden.
    """
    if not step.providers:
        return True
    return provider is not None and provider in step.providers


def step_visible_for_features(
    step: OnboardingStep,
    granted_features: frozenset[str],
) -> bool:
    """Whether a step renders given the workspace features the user granted.

    Feature-agnostic steps (``requires_feature is None``) always render. A
    feature-gated step (e.g. the calendar demo) renders only once that
    feature's scopes appear in the connected workspace's granted-scopes
    secret; before then — or if the user declined the scope at connect — it
    stays hidden.
    """
    if step.requires_feature is None:
        return True
    return step.requires_feature in granted_features


def chip_event_for(step_id: str, chip_id: str) -> OnboardingEventSpec | None:
    """Event fired when the user clicks a graph-owned example chip.

    The instruction is resolved from the canonical presentation chips
    server-side, so the wire payload never carries user-supplied text and
    an unknown ``step_id``/``chip_id`` pair yields ``None`` (the caller then
    emits nothing).
    """
    step = STEP_BY_ID.get(step_id)
    presentation = STEP_PRESENTATION.get(step_id)
    if step is None or presentation is None:
        return None
    chip = next(
        (
            candidate
            for candidate in (*presentation.chips_chat, *presentation.chips_call)
            if candidate.id == chip_id
        ),
        None,
    )
    if chip is None:
        return None
    if step_id == "apps":
        metadata = dict(chip.metadata or {})
        return OnboardingEventSpec(
            event_type="coordinator_onboarding_event",
            message=(
                f'The user picked "{chip.label}" under "{step.title}". Open with '
                "a short nudge that connects this use case to the Integrations "
                "gallery, then let them connect the app in Console. Do not mark "
                "the step complete from this click; it completes only after an "
                "app credential lands."
            ),
            subtype="integration_connect_chip_requested",
            details={
                "trigger_step_id": step_id,
                "chip_id": chip_id,
                "instruction": chip.label,
                **metadata,
                "framing": INTEGRATIONS_FRAMING,
                "phase": PHASE_INTEGRATIONS,
                "phase_id": "integrations",
                "phase_framing": INTEGRATIONS_FRAMING,
                "interaction": {
                    "type": "integration_connect_chip",
                    "trigger_step_id": step_id,
                    "instruction": chip.label,
                    **metadata,
                    "instructions": INTEGRATIONS_FRAMING,
                },
            },
        )
    if step_id in {"integration-read", "integration-action"}:
        return OnboardingEventSpec(
            event_type="coordinator_onboarding_event",
            message=(
                f'The user picked "{chip.label}" under "{step.title}". Treat '
                "the chip label as the demo instruction: use connected app tools "
                "to do it now, send one short user-facing deliverable as a "
                "unify_message. Handling the demo is not finished until that "
                "deliverable has been sent. If no "
                "connected app fits, say exactly what connection is missing and "
                "do not mark the step complete."
            ),
            subtype="integration_demo_chip_requested",
            details={
                "trigger_step_id": step_id,
                "chip_id": chip_id,
                "instruction": chip.label,
                "channel": step.channel,
                "framing": INTEGRATIONS_FRAMING,
                "phase": PHASE_INTEGRATIONS,
                "phase_id": "integrations",
                "phase_framing": INTEGRATIONS_FRAMING,
                "interaction": {
                    "type": "integration_demo_chip",
                    "trigger_step_id": step_id,
                    "instruction": chip.label,
                    "channel": step.channel,
                    "instructions": INTEGRATIONS_FRAMING,
                },
            },
        )
    if step_id not in _TASK_BEAT_KIND:
        return None
    task_kind = _TASK_BEAT_KIND[step_id]
    run_note = (
        " Mention they can trip it right away with the 'Test it' control under "
        f"the row, and {_ACTIONS_TAB_NUDGE}."
        if task_kind == "triggered"
        else f" Since it can fire soon, {_ACTIONS_TAB_NUDGE}."
    )
    return OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f'The user picked the example task "{chip.label}" under '
            f"'{step.title}', so set that up now with your task tools: treat "
            "the example as their instruction, fill in sensible defaults, and "
            "only ask if a genuinely required detail (such as which channel to "
            "reach them on) is missing. Confirm it in one short message, then "
            f"{_TASKS_TAB_NUDGE}.{run_note} "
            "If an equivalent task already exists, treat this as a nudge and "
            "just confirm rather than creating a duplicate."
        ),
        subtype="task_chip_requested",
        details={
            "trigger_step_id": step_id,
            "chip_id": chip_id,
            "instruction": chip.label,
            "task_kind": task_kind,
            "framing": TASKS_FRAMING,
            "phase": PHASE_TASKS,
            "phase_id": "tasks",
            "phase_framing": TASKS_FRAMING,
            "interaction": {
                "type": "task_chip",
                "trigger_step_id": step_id,
                "task_kind": task_kind,
                "instruction": chip.label,
                "instructions": TASKS_FRAMING,
            },
        },
    )


def flow_note_for(step_id: str) -> str:
    """How the user advances one step, owned beside the canonical graph."""
    return STEP_FLOW_NOTES.get(step_id, "")


def phase_is_visible(phase_label: str, *, local_mode: bool) -> bool:
    """Whether a phase — and therefore its steps — renders on this deployment.

    ``local_only`` phases show only on a local self-host install; hosted
    staging/production omit them entirely. Unknown labels stay visible.
    """
    phase = PHASE_BY_LABEL.get(phase_label)
    if phase is None:
        return True
    return local_mode or not phase.local_only


def visible_phases(*, local_mode: bool) -> tuple[OnboardingPhase, ...]:
    """Phase headers visible on this deployment, in display order."""
    return tuple(
        phase for phase in ONBOARDING_PHASES if local_mode or not phase.local_only
    )


def dependencies_satisfied(
    depends_on: dict[str, int],
    completed: set[str],
    skipped: set[str],
) -> bool:
    """Whether every gate on a step is open at its declared level.

    ``COMPLETED`` (1) needs the dependency in ``completed``; ``ADDRESSED``
    (0) also accepts a ``skipped`` dependency. An empty map is always
    satisfied.
    """
    for dep_id, level in depends_on.items():
        if level == COMPLETED:
            if dep_id not in completed:
                return False
        elif dep_id not in completed and dep_id not in skipped:
            return False
    return True


def completion_blocked_descendants(step_id: str) -> tuple[str, ...]:
    """Steps that become unreachable when ``step_id`` is skipped.

    Only ``COMPLETED`` edges cascade: ``ADDRESSED`` edges intentionally accept
    skipped dependencies.
    """
    blocked = {step_id}
    descendants: list[str] = []
    changed = True
    while changed:
        changed = False
        for step in ONBOARDING_GRAPH:
            if step.id in blocked:
                continue
            if any(
                dep_id in blocked and level == COMPLETED
                for dep_id, level in step.depends_on.items()
            ):
                blocked.add(step.id)
                descendants.append(step.id)
                changed = True
    return tuple(descendants)


def dependency_descendants(step_id: str) -> tuple[str, ...]:
    """Steps downstream of ``step_id`` via any ``depends_on`` edge."""
    reachable = {step_id}
    descendants: list[str] = []
    changed = True
    while changed:
        changed = False
        for step in ONBOARDING_GRAPH:
            if step.id in reachable:
                continue
            if any(dep_id in reachable for dep_id in step.depends_on):
                reachable.add(step.id)
                descendants.append(step.id)
                changed = True
    return tuple(descendants)


def completion_required_ancestors(step_id: str) -> tuple[str, ...]:
    """Completion-required prerequisites for ``step_id``, nearest first."""
    ancestors: list[str] = []
    seen: set[str] = set()

    def visit(current_id: str) -> None:
        current = STEP_BY_ID.get(current_id)
        if current is None:
            return
        for dep_id, level in current.depends_on.items():
            if level != COMPLETED or dep_id in seen:
                continue
            seen.add(dep_id)
            ancestors.append(dep_id)
            visit(dep_id)

    visit(step_id)
    return tuple(ancestors)


def completion_coupled_steps(step_id: str) -> tuple[str, ...]:
    """Steps reset together when ``step_id`` is reset in coordinator state."""
    coupled = {step_id, *completion_required_ancestors(step_id)}
    for coupled_id in tuple(coupled):
        coupled.update(dependency_descendants(coupled_id))
    return tuple(step.id for step in ONBOARDING_GRAPH if step.id in coupled)


def manual_completion_block_reason(step_id: str) -> str | None:
    """Return a user-facing reason when Twin must not manually set this step.

    ``None`` means manual completion is allowed. Callers surface the reason
    on attempted PATCH rather than documenting settable steps upfront.
    """
    step = STEP_BY_ID.get(step_id)
    if step is None:
        return "That onboarding step does not exist."
    if step.kind == "coming_soon":
        return "That onboarding step is not available yet."
    if step_id in MANUAL_COMPLETION_STEP_IDS:
        return None
    if step.phase == PHASE_COMMUNICATION:
        return (
            "Communication checklist steps complete automatically when messages "
            "are sent and received on each channel — I cannot mark them done "
            "manually."
        )
    if step.kind == "trigger":
        return (
            "This step starts from the onboarding checklist (or when the user "
            "asks me to begin it) and completes when I perform the action — "
            "I cannot mark it done without doing the work."
        )
    if step.kind == "reply":
        return (
            "This step completes when the user replies on the channel — "
            "I cannot mark it done manually."
        )
    return None


def phase_step_ids_in_graph_order(phase_label: str) -> tuple[str, ...]:
    """Step ids for ``phase_label`` in canonical graph order."""
    return tuple(step.id for step in ONBOARDING_GRAPH if step.phase == phase_label)


def select_primary_next_target_id(
    next_target_ids: set[str],
    *,
    completed: set[str],
    active_step_id: str | None,
) -> str | None:
    """Pick the primary next onboarding target for nudging.

    Default: the first available step in graph order. When the user has
    progress in a phase, prefer continuing that path — the next available
    step after their active step, or after the furthest completed step in
    that phase when no step is active.
    """
    if not next_target_ids:
        return None

    graph_order = [step.id for step in ONBOARDING_GRAPH]
    graph_index = {step_id: index for index, step_id in enumerate(graph_order)}

    def first_in_graph_order(step_ids: set[str]) -> str:
        return min(step_ids, key=lambda step_id: graph_index[step_id])

    focus_phase: str | None = None
    if active_step_id and active_step_id in STEP_BY_ID:
        focus_phase = STEP_BY_ID[active_step_id].phase
    if focus_phase is None:
        focus_phase = STEP_BY_ID[first_in_graph_order(next_target_ids)].phase

    phase_order = phase_step_ids_in_graph_order(focus_phase)
    phase_order_index = {step_id: index for index, step_id in enumerate(phase_order)}

    anchor: str | None = None
    if active_step_id and active_step_id in phase_order_index:
        anchor = active_step_id
    else:
        completed_in_phase = [
            step_id for step_id in phase_order if step_id in completed
        ]
        if completed_in_phase:
            anchor = max(
                completed_in_phase,
                key=lambda step_id: phase_order_index[step_id],
            )

    if anchor is not None:
        if anchor in next_target_ids:
            return anchor
        anchor_index = phase_order_index[anchor]
        for step_id in phase_order[anchor_index + 1 :]:
            if step_id in next_target_ids:
                return step_id

    return first_in_graph_order(next_target_ids)


def order_next_targets(
    next_targets: list[dict[str, Any]],
    *,
    completed: set[str],
    active_step_id: str | None,
) -> list[dict[str, Any]]:
    """Reorder ``next_targets`` so the primary nudge target is first."""
    if len(next_targets) <= 1:
        return next_targets

    target_ids = {target["id"] for target in next_targets}
    graph_index = {step.id: index for index, step in enumerate(ONBOARDING_GRAPH)}
    primary_id = select_primary_next_target_id(
        target_ids,
        completed=completed,
        active_step_id=active_step_id,
    )
    if primary_id is None:
        return next_targets

    by_id = {target["id"]: target for target in next_targets}
    ordered_ids = [primary_id] + sorted(
        target_ids - {primary_id},
        key=lambda step_id: graph_index[step_id],
    )
    return [by_id[step_id] for step_id in ordered_ids]


def _assert_graph_integrity() -> None:
    """Fail loudly on a malformed hand-authored graph.

    Catches the three ways the graph can rot: a dependency id that does
    not exist, a phase that does not exist, and a dependency cycle. Runs
    once at import so a mistake surfaces immediately rather than as a
    confusing empty/locked checklist at runtime.
    """
    for step in ONBOARDING_GRAPH:
        for dep_id, level in step.depends_on.items():
            dep = STEP_BY_ID.get(dep_id)
            if dep is None:
                raise ValueError(
                    f"Onboarding graph: '{step.id}' depends on unknown step '{dep_id}'.",
                )
        if step.phase not in PHASE_BY_LABEL:
            raise ValueError(
                f"Onboarding graph: '{step.id}' has phase '{step.phase}' with no "
                f"registered OnboardingPhase header.",
            )

    unknown_presentation = set(STEP_PRESENTATION) - set(STEP_BY_ID)
    if unknown_presentation:
        raise ValueError(
            f"Onboarding graph: STEP_PRESENTATION has unknown step ids "
            f"{sorted(unknown_presentation)}.",
        )
    unknown_flow_notes = set(STEP_FLOW_NOTES) - set(STEP_BY_ID)
    if unknown_flow_notes:
        raise ValueError(
            f"Onboarding graph: STEP_FLOW_NOTES has unknown step ids "
            f"{sorted(unknown_flow_notes)}.",
        )

    visiting, done = 1, 2
    state: dict[str, int] = {}

    def visit(step_id: str) -> None:
        current = state.get(step_id)
        if current == done:
            return
        if current == visiting:
            raise ValueError(f"Onboarding graph: dependency cycle through '{step_id}'.")
        state[step_id] = visiting
        for dep_id in STEP_BY_ID[step_id].depends_on:
            visit(dep_id)
        state[step_id] = done

    for step in ONBOARDING_GRAPH:
        visit(step.id)


_assert_graph_integrity()
