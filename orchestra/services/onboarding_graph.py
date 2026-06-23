"""Canonical Coordinator onboarding graph — single source of truth.

This module owns the *structure* of Coordinator onboarding: the ordered
set of steps, how they depend on one another, which channel each belongs
to, whether the user can defer it, and the ready-to-use copy the
Coordinator should say to nudge the user toward each one.

It deliberately consolidates what used to be scattered across three
places:
  - Console's ``ONBOARDING_CHECKLIST`` (titles, phases, ``depends_on``).
  - Droid's ``_VOICE_ONBOARDING_STEP_SUGGESTIONS`` /
    ``_VOICE_ONBOARDING_TRIGGER_REPLY_STEPS`` (spoken nudge copy + the
    trigger→reply pairing).
  - The linear ``DERIVABLE_ONBOARDING_STEPS`` tuple in
    ``coordinator_service`` (which steps are server-derivable).

Both Droid brains and the Console checklist consume a rendering computed
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
    "Start onboarding by proving that Twin can communicate with the user "
    "across channels. Frame this as a light sci-fi and pop-culture reference "
    "quiz. There is no fixed list of clues: on each channel Twin invents its "
    "own short quote clue on the spot — a fresh, different reference each time, "
    "Twin's own creative choice — sends it on that channel, the user guesses "
    "the reference there, Twin supports repeats and gentle hints, reveals the "
    "answer if asked or if the user is stuck, then closes naturally before "
    "moving on."
)


@dataclass(frozen=True)
class OnboardingPhase:
    """A checklist phase header — the grouping row shown above its steps.

    ``label`` is the value stamped on each step's ``phase`` field (and the
    short legend label in the progress bar); ``id`` is the stable header-row
    id consumers key off (Console test ids, Droid prose grouping).
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
        "Trigger email from Twin",
        depends_on={},
        channel="email",
        paired_reply="email-reply",
        nudge_chat=(
            "Explain the communication-channel reference quiz, then invite them "
            "to click the 'Trigger email from Twin' row in the Onboarding "
            "checklist for the first clue."
        ),
        nudge_voice=(
            "starting the communication-channel reference quiz by clicking the "
            "'Trigger email from Twin' row in the Onboarding checklist"
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
        "Trigger WhatsApp message from Twin",
        depends_on={"whatsapp-number": COMPLETED},
        channel="whatsapp",
        paired_reply="whatsapp-message",
        nudge_chat=(
            "Invite them to click the 'Trigger WhatsApp message from Twin' row "
            "in the Onboarding checklist to get a clue over WhatsApp."
        ),
        nudge_voice=(
            "clicking the 'Trigger WhatsApp message from Twin' row in the Onboarding checklist"
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
        "Trigger WhatsApp call from Twin",
        depends_on={"whatsapp-number": COMPLETED},
        channel="whatsapp",
        paired_reply="whatsapp-call",
        nudge_chat=(
            "Invite them to click the 'Trigger WhatsApp call from Twin' row in "
            "the Onboarding checklist to get a clue over a WhatsApp call."
        ),
        nudge_voice=(
            "clicking the 'Trigger WhatsApp call from Twin' row in the Onboarding checklist"
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
        "Trigger SMS message from Twin",
        depends_on={"phone-number": COMPLETED},
        channel="sms",
        paired_reply="sms-message",
        nudge_chat=(
            "Invite them to click the 'Trigger SMS message from Twin' row in "
            "the Onboarding checklist to get a clue over SMS."
        ),
        nudge_voice=(
            "clicking the 'Trigger SMS message from Twin' row in the Onboarding checklist"
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
        "Trigger phone call from Twin",
        depends_on={"phone-number": COMPLETED},
        channel="phone",
        paired_reply="phone-call",
        nudge_chat=(
            "Invite them to click the 'Trigger phone call from Twin' row in the "
            "Onboarding checklist to get a clue over a phone call."
        ),
        nudge_voice=(
            "clicking the 'Trigger phone call from Twin' row in the Onboarding checklist"
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
            "it opens the Slack setup path for the Unify Slack app."
        ),
        nudge_voice="clicking the 'Connect Slack' row in the Onboarding checklist",
    ),
    _trigger(
        "slack-reference",
        "Trigger Slack message from Twin",
        depends_on={"slack-connect": COMPLETED},
        channel="slack",
        paired_reply="slack-message",
        nudge_chat=(
            "Invite them to click the 'Trigger Slack message from Twin' row in "
            "the Onboarding checklist to get a clue in Slack."
        ),
        nudge_voice=(
            "clicking the 'Trigger Slack message from Twin' row in the Onboarding checklist"
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
            "it opens the Discord setup path for adding their Discord ID and "
            "installing the public bot."
        ),
        nudge_voice="clicking the 'Connect Discord' row in the Onboarding checklist",
    ),
    _trigger(
        "discord-reference",
        "Trigger Discord message from Twin",
        depends_on={"discord-connect": COMPLETED},
        channel="discord",
        paired_reply="discord-message",
        nudge_chat=(
            "Invite them to click the 'Trigger Discord message from Twin' row in "
            "the Onboarding checklist to get a clue in Discord."
        ),
        nudge_voice=(
            "clicking the 'Trigger Discord message from Twin' row in the Onboarding checklist"
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
        id="schedule",
        title="Schedule a task for later",
        phase=PHASE_TASKS,
        kind="schedule",
        depends_on={},
        can_skip=True,
        derivable=True,
        nudge_chat=(
            "Have them click the 'Schedule a task for later' row in the "
            "Onboarding checklist; it opens Tasks so they can set up recurring "
            "or event-triggered work."
        ),
        nudge_voice=(
            "clicking the 'Schedule a task for later' row in the Onboarding checklist"
        ),
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


_SCHEDULE_CHIPS: tuple[OnboardingChip, ...] = (
    OnboardingChip("morning-briefing", "Send me a briefing tomorrow at 8am"),
    OnboardingChip("weekly-recap", "Every Friday, recap my week"),
    OnboardingChip("email-trigger", "When I get an email from my boss, alert me"),
)

# Presentation copy keyed by step id. Lives beside the graph so every
# consumer (Console checklist, Droid prose) reads the same descriptions,
# time estimates, and suggestion chips from one place.
STEP_PRESENTATION: dict[str, StepPresentation] = {
    "email-reference": StepPresentation(
        "Twin introduces the reference quiz and sends the first clue to your email.",
        "~10s",
    ),
    "email-reply": StepPresentation("Reply to Twin's email with your guess.", "~30s"),
    "whatsapp-number": StepPresentation(
        "Add the WhatsApp number Twin should use.",
        "~30s",
    ),
    "whatsapp-message-reference": StepPresentation(
        "Twin sends the next reference clue over WhatsApp.",
        "~10s",
    ),
    "whatsapp-message": StepPresentation(
        "Reply to Twin's WhatsApp message with your guess.",
        "~1 min",
    ),
    "whatsapp-call-reference": StepPresentation(
        "Twin calls with the next reference clue over WhatsApp.",
        "~10s",
    ),
    "whatsapp-call": StepPresentation(
        "Answer Twin's WhatsApp call and guess the clue.",
        "~1 min",
    ),
    "phone-number": StepPresentation(
        "Add the phone number Twin should use for calls and SMS.",
        "~30s",
    ),
    "sms-reference": StepPresentation(
        "Twin sends the next reference clue over SMS.",
        "~10s",
    ),
    "sms-message": StepPresentation(
        "Reply to Twin's SMS message with your guess.",
        "~1 min",
    ),
    "phone-call-reference": StepPresentation(
        "Twin calls with the next reference clue.",
        "~10s",
    ),
    "phone-call": StepPresentation(
        "Answer Twin's phone call and guess the clue.",
        "~1 min",
    ),
    "slack-connect": StepPresentation(
        "Connect Twin through the Unify Slack app.",
        "~1 min",
    ),
    "slack-reference": StepPresentation(
        "Twin sends the next reference clue in Slack.",
        "~10s",
    ),
    "slack-message": StepPresentation(
        "Reply to Twin's Slack message with your guess.",
        "~1 min",
    ),
    "discord-connect": StepPresentation(
        "Connect Twin through the public Discord bot.",
        "~1 min",
    ),
    "discord-reference": StepPresentation(
        "Twin sends the next reference clue in Discord.",
        "~10s",
    ),
    "discord-message": StepPresentation(
        "Reply to Twin's Discord message with your guess.",
        "~1 min",
    ),
    "workspace": StepPresentation(
        "Required for everything else in onboarding.",
        "~30s",
    ),
    "apps": StepPresentation("Hook up at least one app (Slack, Gmail…).", "~2 min"),
    "schedule": StepPresentation(
        "Set up a recurring or event-triggered task.",
        "~1 min",
        _SCHEDULE_CHIPS,
        _SCHEDULE_CHIPS,
    ),
}

_EMPTY_PRESENTATION = StepPresentation()

STEP_FLOW_NOTES: dict[str, str] = {
    "email-reference": (
        "Clicking the 'Trigger email from Twin' row tells me the user is ready "
        "for the reference-quiz clue over email; if I haven't sent it yet I "
        "introduce the quiz and send my own clue, and if I already have I just "
        "confirm it's on the way rather than sending another."
    ),
    "email-reply": "The user replies with their guess once they receive the email clue.",
    "whatsapp-number": (
        "Clicking the 'Add your WhatsApp number' row opens Account -> Contact "
        "info so the user can add or verify the WhatsApp number."
    ),
    "whatsapp-message-reference": (
        "Clicking the 'Trigger WhatsApp message from Twin' row tells me the "
        "user is ready for the clue over WhatsApp; I send my own clue if I "
        "haven't already, otherwise I just confirm it."
    ),
    "whatsapp-message": "The user guesses the WhatsApp clue.",
    "whatsapp-call-reference": (
        "Clicking the 'Trigger WhatsApp call from Twin' row tells me the user "
        "is ready for a WhatsApp voice clue; I start or request the call unless "
        "I have already done so."
    ),
    "whatsapp-call": "The user guesses during the WhatsApp voice exchange.",
    "phone-number": (
        "Clicking the 'Add your phone number' row opens Account -> Contact info "
        "so the user can add or verify the phone number."
    ),
    "sms-reference": (
        "Clicking the 'Trigger SMS message from Twin' row tells me the user is "
        "ready for the clue by text; I send my own clue if I haven't already, "
        "otherwise I just confirm it."
    ),
    "sms-message": "The user guesses the SMS clue.",
    "phone-call-reference": (
        "Clicking the 'Trigger phone call from Twin' row tells me the user is "
        "ready for a phone-call clue; I start or request the call unless I have "
        "already done so."
    ),
    "phone-call": "The user guesses during the phone call.",
    "slack-connect": (
        "Clicking the 'Connect Slack' row opens the Slack setup path for the "
        "Unify Slack app."
    ),
    "slack-reference": (
        "Clicking the 'Trigger Slack message from Twin' row tells me the user "
        "is ready for the clue in Slack; I send my own clue if I haven't "
        "already, otherwise I just confirm it."
    ),
    "slack-message": "The user guesses the Slack clue.",
    "discord-connect": (
        "Clicking the 'Connect Discord' row opens the Discord setup path for "
        "adding their Discord ID and installing the public Discord bot."
    ),
    "discord-reference": (
        "Clicking the 'Trigger Discord message from Twin' row tells me the "
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
    "apps": (
        "Clicking the 'Connect me with your apps' row opens the Integrations "
        "tab; they connect at least one app from the gallery and authorize it."
    ),
    "schedule": (
        "Clicking the 'Schedule a task for later' row opens the Tasks tab. "
        "Time- or event-bound work lands there and recurs or fires on a trigger. "
        "Read-only suggestion chips render under the schedule row as inspiration only."
    ),
}


def presentation_for(step_id: str) -> StepPresentation:
    """Presentation copy for a step (empty when none is registered)."""
    return STEP_PRESENTATION.get(step_id, _EMPTY_PRESENTATION)


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
