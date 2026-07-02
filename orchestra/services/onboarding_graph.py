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


@dataclass(frozen=True)
class OnboardingChip:
    """A read-only 'try one of these' suggestion shown under a step row.

    ``id`` is a stable key for the UI; ``label`` is the user-facing copy.
    """

    id: str
    label: str


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
PHASE_MY_COMPUTER = "My Computer"
PHASE_YOUR_COMPUTER = "Your Computer"
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
    "unify_message. That delivered message is the proof the demo worked, so it "
    "must be sent as an assistant message back to the user — not merely spoken "
    "on a call. Afterwards T-W1N offers exactly one natural follow-up and only "
    "acts on it if the user says yes: draft a reply to a notable email, suggest "
    "a simple optional way to tidy a messy Drive, or flag a conflict or gap on "
    "the calendar. If the area is empty or T-W1N genuinely cannot read it, it "
    "says so honestly and moves on rather than inventing content."
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
    "point them to the Actions tab, which streams my work live while a task runs"
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
        description="Give me access to your Google or Microsoft workspace.",
        framing=WORKSPACE_FRAMING,
    ),
    OnboardingPhase(
        id="integrations",
        label=PHASE_INTEGRATIONS,
        title="Integrations",
        description="Connect the apps and services I should work with.",
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
        description="Teach me the background I should remember.",
    ),
    OnboardingPhase(
        id="canvas",
        label=PHASE_CANVAS,
        title="Canvas",
        description="Use a shared visual workspace.",
    ),
    OnboardingPhase(
        id="my-computer",
        label=PHASE_MY_COMPUTER,
        title="My Computer",
        description="Ask me to operate from my computer.",
    ),
    OnboardingPhase(
        id="your-computer",
        label=PHASE_YOUR_COMPUTER,
        title="Your Computer",
        description="Let me help on your computer.",
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
) -> OnboardingStep:
    """A workspace demo trigger row.

    Structurally a trigger (clicking it asks Twin to act now) but, unlike the
    reference-quiz triggers, it has no paired reply: the proof of completion is
    the assistant-authored summary delivered back to the user over
    ``unify_message`` (see ``DEMO_TO_OUTBOUND_MEDIUMS``). The ``workspace_demo``
    interaction type lets Unity narrate it differently from a quiz clue.
    """
    interaction = {
        "type": "workspace_demo",
        "trigger_step_id": step_id,
        "channel": channel,
        "instructions": WORKSPACE_FRAMING,
    }
    event = OnboardingEventSpec(
        event_type="coordinator_onboarding_event",
        message=(
            f"The user just clicked '{title}', so they want T-W1N to run this "
            "workspace demo now: read the relevant part of their connected "
            "workspace and send the summary back to them as a unify_message. "
            "This is a poll, not a request to repeat work already done: if the "
            "summary has already been delivered, treat this as confirmation and "
            "do NOT send a duplicate."
        ),
        subtype="workspace_demo_requested",
        details={
            "trigger_step_id": step_id,
            "channel": channel,
            "framing": WORKSPACE_FRAMING,
            "phase": PHASE_WORKSPACE,
            "phase_id": "workspace",
            "phase_framing": WORKSPACE_FRAMING,
            "interaction": interaction,
        },
    )
    return OnboardingStep(
        id=step_id,
        title=title,
        phase=PHASE_WORKSPACE,
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
        id="discord-connect",
        title="Connect Discord",
        phase=PHASE_COMMUNICATION,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        channel="discord",
        nudge_chat=(
            "Have them click the 'Connect Discord' row in the Onboarding checklist; "
            "it walks them through copying their Discord user ID (Developer Mode) "
            "and adding my public bot. Remind them the bot can only DM them once "
            "they share a server with it."
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
        title="Give me access to your workspace",
        phase=PHASE_WORKSPACE,
        kind="connect",
        depends_on={},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Give me access to your workspace' row in the "
            "Onboarding checklist; it opens the Google or Microsoft workspace "
            "connection flow."
        ),
        nudge_voice=(
            "clicking the 'Give me access to your workspace' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-mailbox",
        "Summarise my mailbox",
        channel="workspace_mailbox",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Once their workspace is connected, invite them to click the "
            "'Summarise my mailbox' row in the Onboarding checklist; I read their "
            "recent mail and send back a short summary, then offer to draft a reply."
        ),
        nudge_voice=(
            "clicking the 'Summarise my mailbox' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-drive",
        "Take a look at my files",
        channel="workspace_drive",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Invite them to click the 'Take a look at my files' row in the "
            "Onboarding checklist; I scan their Drive or OneDrive and send back a "
            "short summary, then suggest a simple, optional tidy-up if it looks messy."
        ),
        nudge_voice=(
            "clicking the 'Take a look at my files' row in the Onboarding checklist"
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
    ),
    _demo(
        "workspace-contacts",
        "Check my workspace contacts",
        channel="workspace_contacts",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Invite them to click the 'Check my workspace contacts' row in the "
            "Onboarding checklist; I read their connected workspace contacts and "
            "send back a short summary of who's there."
        ),
        nudge_voice=(
            "clicking the 'Check my workspace contacts' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-tasks",
        "Check my tasks due within a week",
        channel="workspace_tasks",
        depends_on={"workspace": COMPLETED},
        nudge_chat=(
            "Invite them to click the 'Check my tasks due within a week' row in "
            "the Onboarding checklist; I read their connected workspace tasks and "
            "send back a short summary of what's open and due in the next week."
        ),
        nudge_voice=(
            "clicking the 'Check my tasks due within a week' row in the Onboarding checklist"
        ),
    ),
    _demo(
        "workspace-teams",
        "Summarise my Teams messages",
        channel="workspace_teams",
        depends_on={"workspace": COMPLETED},
        providers=("microsoft",),
        nudge_chat=(
            "Invite them to click the 'Summarise my Teams messages' row in the "
            "Onboarding checklist; I read their recent Microsoft Teams chats and "
            "channels and send back a short summary of what needs their attention."
        ),
        nudge_voice=(
            "clicking the 'Summarise my Teams messages' row in the Onboarding checklist"
        ),
    ),
    OnboardingStep(
        id="apps",
        title="Connect me with your apps",
        phase=PHASE_INTEGRATIONS,
        kind="connect",
        depends_on={"workspace": COMPLETED},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Connect me with your apps' row in the "
            "Onboarding checklist; it opens Integrations so they can connect "
            "at least one app (Slack, Gmail, Notion, ...)."
        ),
        nudge_voice=(
            "clicking the 'Connect me with your apps' row in the Onboarding checklist"
        ),
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
    _coming_soon("learning-coming-soon", PHASE_LEARNING),
    _coming_soon("canvas-coming-soon", PHASE_CANVAS),
    _coming_soon("my-computer-coming-soon", PHASE_MY_COMPUTER),
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

# Workspace demo trigger rows have no paired reply: completion is proved by the
# assistant's own summary delivered back to the user over ``unify_message``.
# They derive through the same trigger-outbound path as the reference quiz, so
# they merge into ``TRIGGER_TO_OUTBOUND_MEDIUMS`` and are picked up by
# ``derive_onboarding_progress`` without any extra wiring.
DEMO_TO_OUTBOUND_MEDIUMS: dict[str, tuple[str, ...]] = {
    "workspace-mailbox": ("unify_message",),
    "workspace-drive": ("unify_message",),
    "workspace-calendar": ("unify_message",),
    "workspace-contacts": ("unify_message",),
    "workspace-tasks": ("unify_message",),
    "workspace-teams": ("unify_message",),
}
TRIGGER_TO_OUTBOUND_MEDIUMS.update(DEMO_TO_OUTBOUND_MEDIUMS)

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
    "discord-connect": StepPresentation(
        "Add T-W1N's public Discord bot and share your Discord user ID so it "
        "can DM you.",
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
    "workspace-contacts": StepPresentation(
        "T-W1N reads your connected workspace contacts and sends back a short "
        "summary of who's there.",
        "~30s",
    ),
    "workspace-tasks": StepPresentation(
        "T-W1N reads your connected workspace tasks and sends back a short "
        "summary of what's open and due in the next week.",
        "~30s",
    ),
    "workspace-teams": StepPresentation(
        "T-W1N reads your recent Microsoft Teams chats and channels and sends "
        "back a short summary of what needs your attention.",
        "~30s",
    ),
    "apps": StepPresentation("Hook up at least one app (Slack, Gmail…).", "~2 min"),
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
    "discord-connect": (
        "Clicking the 'Connect Discord' row opens the Discord setup path. Walk "
        "them through it: in Discord, turn on Settings -> Advanced -> Developer "
        "Mode, then right-click their own name and 'Copy User ID' and paste that "
        "into the setup dialog; then add T-W1N's public Discord bot from the "
        "link in the same dialog. The thing that trips people up: the bot can "
        "only DM them once they share a server with it, so if my first Discord "
        "message never arrives that is almost always why -- have them add the "
        "bot to a server they're in and try the clue again."
    ),
    "discord-reference": (
        "Clicking the 'Trigger Discord message from T-W1N' row tells me the "
        "user is ready for the clue in Discord; I send my own clue if I haven't "
        "already, otherwise I just confirm it."
    ),
    "discord-message": "The user guesses the Discord clue.",
    "workspace": (
        "Clicking the 'Give me access to your workspace' row opens the workspace "
        "OAuth dialog (Google Workspace or Microsoft 365). Completing OAuth "
        "grants me access to their email, calendar, files, and other workspace "
        "resources."
    ),
    "workspace-mailbox": (
        "Clicking the 'Summarise my mailbox' row tells me the user wants a live "
        "demo of their connected mailbox: I read their recent mail with my own "
        "tools and deliver one short summary back to them as a single "
        "unify_message, then offer to draft a reply to a notable thread. If I "
        "have already delivered the summary I just confirm it rather than "
        "sending another."
    ),
    "workspace-drive": (
        "Clicking the 'Take a look at my files' row tells me the user wants a "
        "demo of their connected Drive or OneDrive: I read what's there and send "
        "one short summary back as a single unify_message, then offer a simple, "
        "optional way to tidy things up if the files look disorganised. I only "
        "reorganise anything if they say yes."
    ),
    "workspace-calendar": (
        "Clicking the 'Check my upcoming calendar events within a week' row "
        "tells me the user wants a demo of their connected calendar: I read "
        "their events for the next week and send one short summary back as a "
        "single unify_message, flagging any conflicts or gaps."
    ),
    "workspace-contacts": (
        "Clicking the 'Check my workspace contacts' row tells me the user wants "
        "a demo of their connected workspace contacts: I read them and send one "
        "short summary back as a single unify_message. If I have already "
        "delivered the summary I just confirm it rather than sending another."
    ),
    "workspace-tasks": (
        "Clicking the 'Check my tasks due within a week' row tells me the user "
        "wants a demo of their connected workspace tasks: I read what's open and "
        "due in the next week and send one short summary back as a single "
        "unify_message. If I have already delivered the summary I just confirm "
        "it rather than sending another."
    ),
    "workspace-teams": (
        "Clicking the 'Summarise my Teams messages' row tells me the user wants "
        "a demo of their Microsoft Teams messages: I read their recent Teams "
        "chats and channels and send one short summary of what needs their "
        "attention back as a single unify_message. If I have already delivered "
        "the summary I just confirm it rather than sending another. This row "
        "only appears for a connected Microsoft workspace."
    ),
    "apps": (
        "Clicking the 'Connect me with your apps' row opens the Integrations "
        "tab; they connect at least one app from the gallery and authorize it."
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


def chip_event_for(step_id: str, chip_id: str) -> OnboardingEventSpec | None:
    """Event fired when the user clicks a Tasks-phase example chip.

    Unlike a beat row (which asks Twin to open a freeform conversation), a chip
    is a fully-specified example task: the click asks Twin to set that exact
    task up now. The instruction is resolved from the canonical presentation
    chips server-side, so the wire payload never carries user-supplied text and
    an unknown ``step_id``/``chip_id`` pair yields ``None`` (the caller then
    emits nothing).
    """
    step = STEP_BY_ID.get(step_id)
    presentation = STEP_PRESENTATION.get(step_id)
    if step is None or presentation is None or step_id not in _TASK_BEAT_KIND:
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
    """Steps coupled by completed-only dependency edges around ``step_id``."""
    coupled = {step_id, *completion_required_ancestors(step_id)}
    for coupled_id in tuple(coupled):
        coupled.update(completion_blocked_descendants(coupled_id))
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
