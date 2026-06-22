from datetime import datetime
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar
from zoneinfo import available_timezones

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)
from pydantic.generics import GenericModel

from orchestra.web.api.utils.safe_text import (
    MAX_LABEL_LENGTH,
    OptionalSafeLabel,
    OptionalSafeText,
    SafeLabel,
    SafeText,
    validate_safe_text,
)

T = TypeVar("T")

VALID_TIMEZONES = available_timezones()


def _normalize_job_title(v: Optional[str]) -> Optional[str]:
    """Trim whitespace and treat empty strings as ``None``.

    Avoids storing whitespace-only values that would render as a blank
    subtitle in the console without actually conveying anything.
    """
    if v is None:
        return None
    trimmed = v.strip()
    if not trimmed:
        return None
    # Block HTML/script injection in the (displayed) job title.
    return validate_safe_text(trimmed, max_length=MAX_LABEL_LENGTH)


class InfoResponse(GenericModel, Generic[T]):
    """
    Generic wrapper for API responses.
    Wraps any response type under an 'info' key while preserving schema validation.
    """

    info: T


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = Field(
        ...,
        description="The role of the message sender.",
        example="assistant",
    )
    msg: str = Field(
        ...,
        description="The content of the message.",
        example="Hello, how can I help you?",
    )


class UnifyMessage(BaseModel):
    assistant_id: int = Field(..., description="The ID of the assistant to message.")
    contact_id: int = Field(
        ...,
        description="The ID of the contact sending the message. Currently only '1' (the user) is supported.",
        example=1,
    )
    message: str = Field(..., description="The message content.", example="Hello!")


class AssistantCreate(BaseModel):
    """
    Schema for creating a new assistant.

    When this request is made with an organization API key, the assistant is
    created inside that organization but still records the calling user as its
    creator/lifecycle owner. Organization access is layered on through
    ``organization_id`` and org RBAC, not by rewriting ``user_id``.
    """

    first_name: OptionalSafeLabel = Field(
        None,
        description="First name of the assistant",
        example="Ada",
    )
    surname: OptionalSafeLabel = Field(
        None,
        description="Surname of the assistant",
        example="Lovelace",
    )
    job_title: Optional[str] = Field(
        None,
        description=(
            "Free-text job title or specialization for the assistant "
            "(e.g. 'Growth marketing', 'QA engineer')."
        ),
        example="Senior Mathematician",
        max_length=120,
    )
    age: Optional[int] = Field(
        None,
        description="Age of the assistant",
        example=28,
    )
    weekly_limit: Optional[float] = Field(
        None,
        description="Weekly time limit for the assistant in hours",
        example=15.75,
    )
    max_parallel: Optional[int] = Field(
        None,
        description="Maximum number of parallel tasks the assistant can handle",
        example=2,
    )
    nationality: Optional[str] = Field(
        None,
        description="Assistant's nationality",
        example="North America",
    )
    profile_photo: Optional[str] = Field(
        None,
        description="URL to the assistant's profile photo",
        example="https://example.com/photos/ada.jpg",
    )
    profile_video: Optional[str] = Field(
        None,
        description="URL to the assistant's profile video",
        example="https://example.com/videos/ada.mp4",
    )
    desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Desktop operating system mode for assistant's VM type",
        example="windows",
    )
    about: OptionalSafeText = Field(
        None,
        description="Brief description about the assistant",
        example="Mathematician and writer known for work on Analytical Engine",
    )
    # NOTE: Contact fields (email, user_phone, user_whatsapp_number, phone_country)
    # have been removed from AssistantCreate.  Contact provisioning is now handled
    # exclusively through the dedicated POST /assistant/{id}/contact endpoint.
    voice_id: Optional[str] = Field(
        None,
        description="Id of the provider voice to use for the assistant",
        example="bf0a246a-8642-498a-9950-80c35e9276b5",
    )
    voice_provider: Optional[str] = Field(
        None,
        description="Provider of the selected voice (e.g., 'elevenlabs', 'openai')",
        example="elevenlabs",
    )
    create_infra: Optional[bool] = Field(
        True,
        description="Whether to create the infrastructure for the assistant (pubsub, VM, etc.)",
        exclude=True,
    )
    is_local: Optional[bool] = Field(
        False,
        description=(
            "Whether this is a local assistant (runs droid locally instead of on GKE). "
            "Local assistants skip wakeup calls and GKE job management in the adapters."
        ),
    )
    is_coordinator: Optional[bool] = Field(
        None,
        description=(
            "Reserved for coordinator bootstrap endpoints. "
            "Generic assistant creation must not include this field."
        ),
        exclude=True,
    )
    pre_hire_chat: Optional[List[ChatMessage]] = Field(
        None,
        description="A list of chat messages from the pre-hire conversation to be logged.",
    )
    timezone: Optional[str] = Field(
        None,
        description="Timezone of the assistant in IANA format",
        example="America/New_York",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v

    @field_validator("job_title")
    @classmethod
    def normalize_job_title(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_job_title(v)

    @model_validator(mode="after")
    def check_voice_fields(cls, self):
        voice_id, voice_provider = (
            self.voice_id,
            self.voice_provider,
        )

        # If any voice field is provided, id and provider are required.
        if any(v is not None for v in [voice_id, voice_provider]):
            if voice_id is None or voice_provider is None:
                raise ValueError(
                    "If providing voice information, both 'voice_id' and 'voice_provider' are required.",
                )
        # AssistantRead extends AssistantCreate for response shaping, so this
        # guard must only apply to create payload validation.
        if (
            self.is_coordinator is not None
            and self.__class__.__name__ == "AssistantCreate"
        ):
            raise ValueError(
                "'is_coordinator' is not accepted on this endpoint. "
                "Use POST /user/{user_id}/coordinator instead.",
            )
        return self

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "job_title": "Senior Mathematician",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "nationality": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada.mp4",
                "desktop_mode": "windows",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "timezone": "America/New_York",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "voice_provider": "cartesia",
            },
        },
    )


class ConsoleConfigRead(BaseModel):
    """Nested representation of ``AssistantConsoleConfig`` for API responses."""

    version: str = "1"
    layout: Dict[str, Any]
    tabs: Optional[Dict[str, Any]] = None
    theme: Optional[Dict[str, Any]] = None


class AssistantTeamSummary(BaseModel):
    """Organization team metadata projected onto assistant runtime responses."""

    team_id: int = Field(..., description="Organization team identifier.")
    name: str = Field(..., description="Human-readable team name.")
    description: Optional[str] = Field(
        None,
        description="Semantic description of the team's purpose and scope.",
    )


class AssistantContactIdentityRoot(BaseModel):
    """Root-local contact ids used by clients that read across assistant roots."""

    target_scope: Literal["personal", "team"] = Field(
        ...,
        description="Root kind where the contact ids are meaningful.",
    )
    target_team_id: Optional[int] = Field(
        None,
        description="Organization team identifier when the target scope is a team.",
    )
    self_contact_id: int = Field(
        ...,
        description="Contact id representing the assistant inside this root.",
    )
    boss_contact_id: int = Field(
        ...,
        description="Contact id representing the assistant owner inside this root.",
    )


class AssistantUserDesktopLink(BaseModel):
    """A single user's desktop linked to an assistant (admin/runtime view)."""

    owner_user_id: str = Field(..., description="User who owns the linked desktop")
    url: str = Field(..., description="Public tunnel URL of the linked desktop")
    os: str = Field(..., description="Operating system of the linked desktop")
    filesys_sync: bool = Field(..., description="Whether filesystem sync is enabled")
    sftp_tunnel_host: Optional[str] = Field(
        None,
        description="Host for on-demand SFTP access to the user's home",
    )
    sftp_tunnel_port: Optional[int] = Field(
        None,
        description="Port for on-demand SFTP access to the user's home",
    )


class AssistantRead(AssistantCreate):
    """
    Schema for reading assistant data, extends AssistantCreate with additional fields.

    For organization-scoped assistants, ``user_id`` is the creator/lifecycle
    owner and ``organization_id`` is the org access scope.
    """

    about: Optional[str] = Field(
        None,
        description=(
            "Description of the assistant. Read responses may include longer "
            "system-authored bios, such as the canonical Coordinator persona."
        ),
    )

    user_desktop_url: Optional[str] = Field(
        None,
        description=(
            "Resolved URL of the requesting user's own desktop linked to this "
            "assistant (from device registry), or null if they haven't linked one"
        ),
        example="https://abc123.tunnel.unify.ai",
    )
    user_desktop_filesys_sync: Optional[bool] = Field(
        None,
        description="Whether filesystem sync is enabled for the requesting user's linked desktop",
        example=False,
    )
    user_desktops: List[AssistantUserDesktopLink] = Field(
        default_factory=list,
        description=(
            "All per-user desktops linked to this assistant (admin/runtime only). "
            "Maps each user to their own machine for an assistant several users share."
        ),
    )
    # Admin/runtime only; deliberately NOT part of user_desktops so the private
    # keys never reach the assistant pod env.
    user_desktop_filesync_keys: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-user private SSH keys (keyed by owner_user_id) for on-demand "
            "user-home access. Populated only in admin/runtime responses."
        ),
    )
    user_desktop_mode: Optional[str] = Field(
        None,
        description="Resolved OS of the requesting user's own desktop linked to this assistant",
        example="macos",
    )
    agent_id: str = Field(
        ...,
        description="Unique identifier for the assistant",
        example="12345",
    )
    user_id: str = Field(
        ...,
        description=(
            "ID of the creating user. For org assistants this remains the "
            "creator/lifecycle owner, while org access is governed separately "
            "by organization_id and RBAC."
        ),
        example="123",
    )
    organization_id: Optional[int] = Field(
        None,
        description=(
            "Organization access scope for org assistants. Null means the "
            "assistant is personal rather than organization-scoped."
        ),
        example=None,
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when the assistant was created",
        example="2025-04-25T10:30:00Z",
    )
    updated_at: Optional[datetime] = Field(
        None,
        description="Timestamp when the assistant was last updated",
        example="2025-04-26T14:15:00Z",
    )
    # Contact fields populated from the assistant_contacts table via
    # ``_build_assistant_read``.  These were removed from ``AssistantCreate``
    # (contacts are provisioned through the dedicated POST endpoint) but
    # must remain on the read schema so the API response includes them.
    phone: Optional[str] = Field(
        None,
        description="Phone number of the assistant",
        example="+15551234567",
    )
    phone_country: Optional[str] = Field(
        None,
        description="Country code for the provisioned phone number",
        example="US",
    )
    email: Optional[str] = Field(
        None,
        description="Email address of the assistant",
        example="ada.lovelace@unify.ai",
    )
    email_provider: Optional[Literal["google_workspace", "microsoft_365"]] = Field(
        None,
        description="Provider used for the provisioned email. "
        "None when no email contact is provisioned.",
        example="google_workspace",
    )
    user_phone: Optional[str] = Field(
        None,
        description="User's personal phone number",
        example="+15559876543",
    )
    user_whatsapp_number: Optional[str] = Field(
        None,
        description="User's WhatsApp number",
        example="+15559876543",
    )
    assistant_whatsapp_number: Optional[str] = Field(
        None,
        description="WhatsApp number of the assistant",
        example="+15551234567",
    )
    user_discord_id: Optional[str] = Field(
        None,
        description="User's linked Discord user ID",
    )
    assistant_discord_bot_id: Optional[str] = Field(
        None,
        description="Discord bot ID assigned to the assistant",
    )
    api_key: Optional[str] = Field(
        None,
        description="API key associated with this assistant (personal or org key)",
        example="1234567890",
    )
    user_first_name: Optional[str] = Field(
        None,
        description="First name of the user",
        example="Ada",
    )
    user_last_name: Optional[str] = Field(
        None,
        description="Last name of the user",
        example="Lovelace",
    )
    user_email: Optional[str] = Field(
        None,
        description="Email of the user",
        example="ada.lovelace@unify.ai",
    )
    user_image: Optional[str] = Field(
        None,
        description="Profile image URL of the user (owner/supervisor)",
    )
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="Monthly spending limit in dollars for this assistant.",
        example=100.00,
    )
    demo_id: Optional[int] = Field(
        None,
        description="ID of demo metadata if this is a demo assistant, None for regular assistants.",
        example=None,
    )
    is_local: Optional[bool] = Field(
        None,
        description="Whether this is a local assistant (runs droid locally instead of on GKE).",
    )
    is_coordinator: bool = Field(
        False,
        description="Whether this assistant configures and coordinates its workspace.",
    )
    desktop_filesync_sshkey: Optional[str] = Field(
        None,
        description="SSH private key for desktop filesystem sync. Only returned via admin endpoints.",
    )
    team_ids: List[int] = Field(
        default_factory=list,
        description="Sorted organization team IDs where the assistant is a live shared-memory member.",
    )
    team_summaries: List[AssistantTeamSummary] = Field(
        default_factory=list,
        description="Sorted organization team names and descriptions for live memberships.",
    )
    self_contact_id: int = Field(
        0,
        description="Resolved Contacts row ID representing the assistant itself.",
    )
    boss_contact_id: int = Field(
        1,
        description="Resolved Contacts row ID representing the assistant owner.",
    )
    contact_identity_roots: List[AssistantContactIdentityRoot] = Field(
        default_factory=list,
        description=(
            "Resolved self and boss contact ids for each readable root. "
            "Contact ids are root-local, so clients must use the entry matching "
            "the context they query."
        ),
    )
    secrets: Optional[Dict[str, str]] = Field(
        None,
        description="External service credentials (OAuth tokens, etc.). "
        "Only populated in admin responses.",
    )
    console_config: Optional[ConsoleConfigRead] = Field(
        None,
        description="Per-assistant UI/UX configuration for forward-deployed Console views. "
        "Null means the assistant uses default console behavior.",
    )

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        json_schema_extra={
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "job_title": "Senior Mathematician",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "nationality": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada.mp4",
                "desktop_mode": "windows",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "phone_country": "US",
                "timezone": "America/New_York",
                "email": "ada.lovelace@unify.ai",
                "phone": "+15551234567",
                "user_phone": "+15551234567",
                "user_whatsapp_number": "+15551234567",
                "assistant_whatsapp_number": "+15551234567",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "voice_provider": "cartesia",
                "agent_id": "12345",
                "user_id": "123",
                "organization_id": None,
                "created_at": "2025-04-25T10:30:00Z",
                "updated_at": "2025-04-26T14:15:00Z",
                "api_key": "1234567890",
                "user_first_name": "Ada",
                "user_last_name": "Lovelace",
                "user_email": "ada.lovelace@unify.ai",
                "user_image": "https://example.com/photo.jpg",
                "team_ids": [101, 205],
                "team_summaries": [
                    {
                        "team_id": 101,
                        "name": "Support Ops",
                        "description": "Daily customer support operations and escalation notes.",
                    },
                ],
                "self_contact_id": 42,
                "boss_contact_id": 43,
                "is_local": False,
                "is_coordinator": False,
            },
        },
    )


class CoordinatorTranscriptSeed(BaseModel):
    """Request body for persisting the Coordinator's opener transcript row."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1)
    source_assistant_id: Optional[str] = Field(None)


class CoordinatorTranscriptSeedResponse(BaseModel):
    """Response returned after the opener row is present in the transcript."""

    log_event_id: int


class OnboardingSessionStarted(BaseModel):
    """Request body for the picker-resolution onboarding event.

    Console POSTs this the moment the user picks "I'd rather chat"
    or "Start Call" in the Coordinator onboarding picker. The body
    is intentionally tiny — the server derives the completed-step
    snapshot itself (``derive_onboarding_progress``) and Droid reads
    ``Coordinator/State`` plus the chat-history snapshot when
    generating the opener, so only the medium needs to travel.
    """

    model_config = ConfigDict(extra="forbid")

    medium: Literal["chat", "call"]


class OnboardingSessionStartedResponse(BaseModel):
    """Acknowledgement returned to Console.

    ``emitted`` reports whether the event actually went out — it'll
    be ``False`` when the Coordinator is no longer in onboarding
    mode (e.g. the user already finished or skipped), in which case
    we silently drop the event server-side.
    """

    coordinator_id: str
    emitted: bool


class CoordinatorDelegateRequest(BaseModel):
    """Request body for assigning asynchronous work to a colleague."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(..., min_length=1)
    intent: str = Field("general", min_length=1)
    dedupe_key: Optional[str] = Field(None, min_length=1)
    related_context: Optional[Dict[str, Any]] = None

    @field_validator("instruction", "intent")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped

    @field_validator("dedupe_key")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped


class CoordinatorDelegateResponse(BaseModel):
    """Response returned after a colleague delegation is dispatched."""

    coordinator_id: int
    target_assistant_id: int
    status: str
    activation_id: Optional[str] = None
    accepted: bool = True
    completion_status: str = "pending_async"
    receipt_type: str = "async_delegation_receipt"
    message: str = (
        "The colleague has been woken or notified with the assignment. "
        "This does not mean the colleague has already created durable artifacts "
        "or completed the work."
    )


class CoordinatorResetResponse(BaseModel):
    """Response returned after Coordinator-owned conversation state is reset."""

    coordinator_id: str


class CoordinatorStateUpdate(BaseModel):
    """Request body for transitioning a Coordinator's onboarding state.

    All fields are optional: a request specifying only ``mode`` flips
    the lifecycle without touching the current step; specifying only
    ``onboarding_step`` advances the in-flight step marker without
    leaving ``onboarding``. Passing ``clear_onboarding_step=True``
    resets the step (used when moving to ``working`` so a future
    re-entry doesn't carry stale step state). ``skip_onboarding_step``
    records an intentional user skip separately from real completion;
    ``unskip_onboarding_step`` returns that step to the active checklist.
    ``skip_onboarding_phase`` records a section-level defer without
    expanding it into per-step skips; ``unskip_onboarding_phase`` resumes
    that section while preserving any per-step skips inside it.

    ``intro_watched`` records that the user has resolved the opening
    picker (started the call or chose chat) so the ringing picker and
    auto-playing intro never re-appear on a later page load. It is
    one-way sticky: once ``True`` it cannot be reset to ``False``.

    ``onboarding_deferred`` is the global "do onboarding later" switch.
    Setting it ``True`` suppresses every onboarding narration/opener
    event and the server-side step derivation exactly as if onboarding
    were complete, without touching ``mode`` or any per-step state, so
    the user can start using the platform first. It is freely
    reversible: setting it back to ``False`` resumes the flow untouched.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Optional[Literal["onboarding", "working"]] = Field(None)
    onboarding_step: Optional[str] = Field(None, min_length=1)
    clear_onboarding_step: bool = Field(False)
    skip_onboarding_step: Optional[str] = Field(None, min_length=1)
    unskip_onboarding_step: Optional[str] = Field(None, min_length=1)
    skip_onboarding_phase: Optional[str] = Field(None, min_length=1)
    unskip_onboarding_phase: Optional[str] = Field(None, min_length=1)
    intro_watched: Optional[bool] = Field(None)
    onboarding_deferred: Optional[bool] = Field(None)


class OnboardingChip(BaseModel):
    """A read-only suggestion chip shown under the act/schedule rows."""

    id: str
    label: str


class OnboardingPhaseInfo(BaseModel):
    """A checklist phase header (grouping row) with its display copy.

    ``id`` is the stable header-row id (``comms`` / ``connect`` / ``work``);
    ``phase`` is the label stamped on each step in the phase (and the short
    progress-bar legend). Only phases visible on this deployment are sent —
    ``local_only`` phases are omitted on hosted staging/production.
    """

    id: str
    phase: str
    title: str
    description: str = ""


class OnboardingStepStatus(BaseModel):
    """One onboarding step with its resolved status and presentation copy.

    ``status`` is one of ``done`` / ``skipped`` / ``available`` /
    ``locked`` — computed server-side from the canonical graph so
    consumers never re-derive it. ``description`` / ``estimated_time`` /
    ``chips_*`` carry the per-step display copy from the canonical graph so
    Console renders straight from this payload without its own copy.
    """

    id: str
    title: str
    phase: str
    status: str
    can_skip: bool = False
    description: str = ""
    estimated_time: str = ""
    chips_chat: List[OnboardingChip] = Field(default_factory=list)
    chips_call: List[OnboardingChip] = Field(default_factory=list)


class OnboardingNextTarget(BaseModel):
    """A step the Coordinator may nudge toward right now.

    Carries ready-to-use copy so neither brain has to phrase the nudge
    itself. ``channel`` is set for quiz steps (email/whatsapp/sms/phone/
    slack/discord) and ``None`` for workspace/apps/act/schedule.
    """

    id: str
    title: str
    nudge_chat: str
    nudge_voice: str
    channel: Optional[str] = None


class OnboardingRender(BaseModel):
    """Precomputed onboarding picture shared by both brains and Console."""

    active_step_id: Optional[str] = None
    phases: List[OnboardingPhaseInfo] = Field(default_factory=list)
    steps: List[OnboardingStepStatus] = Field(default_factory=list)
    next_targets: List[OnboardingNextTarget] = Field(default_factory=list)
    skipped_phase_ids: List[str] = Field(default_factory=list)


class OnboardingCatalogStep(BaseModel):
    """One step in the static onboarding catalog (no per-user status)."""

    id: str
    title: str
    phase: str
    kind: str
    channel: Optional[str] = None
    can_skip: bool = False
    description: str = ""
    estimated_time: str = ""
    chips_chat: List[OnboardingChip] = Field(default_factory=list)
    chips_call: List[OnboardingChip] = Field(default_factory=list)


class OnboardingCatalog(BaseModel):
    """Static, deployment-gated onboarding structure + copy.

    The single source of truth for the *shape* of onboarding, independent
    of any user's progress. Consumers (Console checklist, Droid prose) read
    phase/step copy from here; ``local_only`` phases are already omitted on
    hosted deployments.
    """

    phases: List[OnboardingPhaseInfo] = Field(default_factory=list)
    steps: List[OnboardingCatalogStep] = Field(default_factory=list)


class CoordinatorStateResponse(BaseModel):
    """Snapshot of the latest Coordinator/State row.

    ``completed_step_ids`` is not stored on the row — it is derived
    from durable domain state on every read (communication transcripts,
    profile contact fields, Slack/Discord setup, workspace email contact,
    integration secrets, action history, Tasks rows) so consumers see steps
    completed in earlier sessions without any transition event. Always ``[]``
    outside onboarding mode, where derivation is skipped.
    """

    coordinator_id: int
    mode: Literal["onboarding", "working"]
    onboarding_step: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    completed_step_ids: List[str] = Field(default_factory=list)
    skipped_step_ids: List[str] = Field(default_factory=list)
    skipped_phase_ids: List[str] = Field(default_factory=list)
    intro_watched: bool = False
    onboarding_deferred: bool = False
    # Precomputed depends_on-aware rendering (steps + statuses + valid
    # next targets with nudge copy). Present only while actively
    # onboarding; ``None`` once complete, working, or deferred.
    onboarding: Optional[OnboardingRender] = None


class DemoAssistantCreate(BaseModel):
    """
    Schema for creating a demo assistant.

    Demo assistants are used by Unify employees to demonstrate the product
    to prospects who haven't signed up yet. They are cloned from a source
    assistant and configured for phone-only demo calls.
    """

    source_assistant_id: int = Field(
        ...,
        description="ID of the assistant to clone configuration from",
        example=12345,
    )
    label: SafeLabel = Field(
        ...,
        description="Human-readable label for this demo (e.g., 'Richard Branson demo')",
        example="Richard Branson demo",
    )
    first_name: SafeLabel = Field(
        ...,
        description="First name of the demo assistant",
        example="Lucy",
    )
    surname: SafeLabel = Field(
        ...,
        description="Surname of the demo assistant",
        example="Branson-Demo",
    )
    demoer_phone: str = Field(
        ...,
        description="Phone number of the demoer (used as user_phone for contact validation)",
        example="+14155559999",
    )
    monthly_spending_cap: Optional[float] = Field(
        default=10.0,
        ge=1.0,
        le=100.0,
        description="Monthly spending cap in USD for the demo assistant (default: $10, max: $100)",
        example=10.0,
    )
    phone_country: Optional[str] = Field(
        None,
        description="Country code for phone number provisioning (e.g., US, GB). If not provided, uses source assistant's country or defaults to US.",
        example="US",
    )
    # Optional prospect details - if provided, Droid will pre-populate the boss contact
    prospect_first_name: OptionalSafeLabel = Field(
        None,
        description="Prospect's first name (optional, for pre-populating boss contact in Droid)",
        example="Richard",
    )
    prospect_surname: OptionalSafeLabel = Field(
        None,
        description="Prospect's surname (optional, for pre-populating boss contact in Droid)",
        example="Branson",
    )
    prospect_email: Optional[str] = Field(
        None,
        description="Prospect's email address (optional, for pre-populating boss contact in Droid)",
        example="richard@virgin.com",
    )
    prospect_phone: Optional[str] = Field(
        None,
        description="Prospect's phone number in E.164 format (optional, for pre-populating boss contact in Droid)",
        example="+447700900000",
    )

    class Config:
        schema_extra = {
            "example": {
                "source_assistant_id": 12345,
                "label": "Richard Branson demo",
                "first_name": "Lucy",
                "surname": "Branson-Demo",
                "demoer_phone": "+14155559999",
                "monthly_spending_cap": 10.0,
                "prospect_first_name": "Richard",
                "prospect_surname": "Branson",
                "prospect_email": "richard@virgin.com",
                "prospect_phone": "+447700900000",
            },
        }


class DemoAssistantMetaRead(BaseModel):
    """
    Schema for reading demo assistant metadata.
    """

    id: int = Field(
        ...,
        description="Unique identifier for the demo metadata",
        example=42,
    )
    source_assistant_id: Optional[int] = Field(
        None,
        description="ID of the assistant this demo was cloned from (may be None if source was deleted)",
        example=12345,
    )
    demoer_user_id: str = Field(
        ...,
        description="ID of the user who created this demo assistant",
        example="user_abc123",
    )
    label: str = Field(
        ...,
        description="Human-readable label for this demo",
        example="Richard Branson demo",
    )
    created_at: datetime = Field(
        ...,
        description="When the demo assistant was created",
        example="2026-02-10T14:30:00Z",
    )
    # Optional prospect details - stored if provided during creation
    prospect_first_name: Optional[str] = Field(
        None,
        description="Prospect's first name (if provided during creation)",
        example="Richard",
    )
    prospect_surname: Optional[str] = Field(
        None,
        description="Prospect's surname (if provided during creation)",
        example="Branson",
    )
    prospect_email: Optional[str] = Field(
        None,
        description="Prospect's email address (if provided during creation)",
        example="richard@virgin.com",
    )
    prospect_phone: Optional[str] = Field(
        None,
        description="Prospect's phone number in E.164 format (if provided during creation)",
        example="+447700900000",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "id": 42,
                "source_assistant_id": 12345,
                "demoer_user_id": "user_abc123",
                "label": "Richard Branson demo",
                "created_at": "2026-02-10T14:30:00Z",
                "prospect_first_name": "Richard",
                "prospect_surname": "Branson",
                "prospect_email": "richard@virgin.com",
                "prospect_phone": "+447700900000",
            },
        }


class AssistantUpdate(BaseModel):
    """
    Schema for updating an existing assistant.
    Only includes fields that can be updated.
    """

    first_name: OptionalSafeLabel = Field(
        None,
        description="First name of the assistant",
        example="Ada",
    )
    surname: OptionalSafeLabel = Field(
        None,
        description="Surname of the assistant",
        example="Lovelace",
    )
    job_title: Optional[str] = Field(
        None,
        description=(
            "Free-text job title or specialization for the assistant "
            "(e.g. 'Growth marketing', 'QA engineer'). Send an empty string or "
            "null to clear."
        ),
        example="Senior Mathematician",
        max_length=120,
    )
    age: Optional[int] = Field(
        None,
        description="Age of the assistant",
        example=28,
    )
    nationality: Optional[str] = Field(
        None,
        description="Assistant's nationality",
        example="North America",
    )
    weekly_limit: Optional[float] = Field(
        None,
        description="Weekly time limit for the assistant in hours",
        example=20.5,
    )
    max_parallel: Optional[int] = Field(
        None,
        description="Maximum number of parallel tasks the assistant can handle",
        example=3,
    )
    profile_photo: Optional[str] = Field(
        None,
        description="URL to the assistant's profile photo",
        example="https://example.com/photos/ada.jpg",
    )
    profile_video: Optional[str] = Field(
        None,
        description="URL to the assistant's profile video",
        example="https://example.com/videos/ada_new.mp4",
    )
    desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Desktop operating system mode for VM type",
        example="macos",
    )
    about: OptionalSafeText = Field(
        None,
        description="Brief description about the assistant",
        example="Award-winning mathematician specializing in algorithm development",
    )
    # --- DEPRECATED contact fields ---------------------------------------------------
    # Contact provisioning is now handled via the dedicated
    # POST/PUT/DELETE /assistant/{id}/contact endpoints.
    # These fields are retained for backward compatibility but are **ignored**
    # during assistant updates.  They will be removed in a future API version.
    user_phone: Optional[str] = Field(
        None,
        description="DEPRECATED – use POST /assistant/{id}/contact instead.",
        example="+15551234567",
        json_schema_extra={"deprecated": True},
    )
    phone: Optional[str] = Field(
        None,
        description="DEPRECATED – use POST /assistant/{id}/contact instead.",
        example="+15559876543",
        json_schema_extra={"deprecated": True},
    )
    phone_country: Optional[str] = Field(
        None,
        description="DEPRECATED – use POST /assistant/{id}/contact instead.",
        example="GB",
        json_schema_extra={"deprecated": True},
    )
    email: Optional[str] = Field(
        None,
        description="DEPRECATED – use POST /assistant/{id}/contact instead.",
        example="ada.lovelace@newdomain.com",
        json_schema_extra={"deprecated": True},
    )
    user_whatsapp_number: Optional[str] = Field(
        None,
        description="DEPRECATED – use POST /assistant/{id}/contact instead.",
        example="+15559876543",
        json_schema_extra={"deprecated": True},
    )
    voice_id: Optional[str] = Field(  # This is Cartesia's voice ID
        None,
        description="Id of the voice (Cartesia ID) to use for the assistant",
        example="bf0a246a-8642-498a-9950-80c35e9276b5",
    )
    voice_provider: Optional[str] = Field(
        None,
        description="Provider of the selected voice (e.g., 'elevenlabs', 'openai')",
        example="elevenlabs",
    )
    timezone: Optional[str] = Field(
        None,
        description="Timezone of the assistant in IANA format",
        example="Europe/London",
    )
    create_infra: Optional[bool] = Field(
        True,
        description="Whether to create infrastructure for the assistant during update (e.g., phone, email). Set to false for testing.",
        exclude=True,
    )
    is_local: Optional[bool] = Field(
        None,
        description="Whether this is a local assistant (runs droid locally instead of on GKE).",
    )
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="Monthly spending limit in dollars. Set to null to remove the limit.",
        example=100.00,
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v

    @field_validator("job_title")
    @classmethod
    def normalize_update_job_title(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_job_title(v)

    @model_validator(mode="after")
    def check_voice_fields_on_update(cls, self):
        """Validate voice fields for PATCH operations."""
        provided = self.__pydantic_fields_set__

        has_id = "voice_id" in provided
        has_provider = "voice_provider" in provided

        # No voice fields provided, nothing to do
        if not any([has_id, has_provider]):
            return self

        # Clearing voice by sending "voice_id": null
        if has_id and self.voice_id is None:
            self.voice_provider = None
            return self

        # Setting/updating voice: if one of id/provider is given, both must be.
        if has_id or has_provider:
            if not (has_id and has_provider):
                raise ValueError(
                    "To set or update voice information, both 'voice_id' and 'voice_provider' must be provided together.",
                )

            # Since 'has_id' is true, and we passed the 'clearing' check, self.voice_id is not None.
            # We just need to check if self.voice_provider is not None.
            if self.voice_provider is None:
                raise ValueError(
                    "'voice_provider' cannot be null when setting a voice.",
                )

        return self

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        json_schema_extra={
            "example": {
                "job_title": "Senior Mathematician",
                "weekly_limit": 20.5,
                "max_parallel": 3,
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada_new.mp4",
                "desktop_mode": "macos",
                "about": "Award-winning mathematician specializing in algorithm development",
                "user_phone": "+15551234567",
                "phone": "+15559876543",
                "user_whatsapp_number": "+15559876543",
                "assistant_whatsapp_number": "+15559876543",
                "email": "ada.lovelace@newdomain.com",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "voice_provider": "cartesia",
                "phone_country": "GB",
                "timezone": "Europe/London",
            },
        },
    )


class AssistantStatus(BaseModel):
    """
    Schema for the status response from an assistant's running service.
    """

    running: bool = Field(
        ...,
        description="Whether the assistant service process is currently running.",
    )
    job_name: Optional[str] = Field(
        None,
        description="Name of the job running the assistant service.",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example_running": {
                "running": True,
                "job_name": "assistant_service_123",
            },
            "example_inactive": {
                "running": False,
                "job_name": None,
            },
        }


class VoiceCreate(BaseModel):
    """
    Schema for creating a new assistant voice entry in our DB.
    The voice_id is provided by the provider.
    """

    voice_id: str = Field(
        ...,
        description="Provider Voice ID",
        example="bf0a246a-8642-498a-9950-80c35e9276b5",
    )
    name: SafeLabel = Field(
        ...,
        description="User-given name for the voice",
        example="English Woman Calm 1",
    )
    description: SafeText = Field(
        ...,
        description="Description of the voice",
        example="Calm and relaxing voice of an english-speaking woman",
    )
    gender: Optional[str] = Field(
        None,
        description="Gender of the voice",
        example="female",
    )
    language: str = Field(
        ...,
        description="Language code of the voice",
        example="en",
    )
    provider: Literal["cartesia", "elevenlabs", "openai"] = Field(
        "cartesia",
        description="Provider of the voice (cartesia, elevenlabs or openai)",
        example="cartesia",
    )
    is_preset: Optional[bool] = Field(
        False,
        description="Whether this voice is a preset or user-created voice.",
        example=True,
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "name": "English Woman Calm 1",
                "description": "Calm and relaxting voice of an english-speaking woman",
                "gender": "female",
                "language": "en",
                "provider": "cartesia",
                "is_preset": True,
            },
        }


class VoiceRead(VoiceCreate):
    """
    Schema for reading voice data from the DB.
    """

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "name": "English Woman Calm 1",
                "description": "Calm and relaxting voice of an english-speaking woman",
                "gender": "female",
                "language": "en",
                "provider": "cartesia",
                "is_preset": True,
            },
        }


class VoiceCloneRequestData(BaseModel):
    name: SafeLabel = Field(..., description="Name for the new cloned voice")
    language: str = Field(..., description="Language of the audio clip (e.g., 'en')")
    description: OptionalSafeText = Field(
        None,
        description="Optional description for the voice",
    )


class VoiceGenerateRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize.", max_length=5000)
    provider: Literal["cartesia", "elevenlabs", "openai"] = Field(
        ...,
        description="TTS provider.",
    )
    voice_id: str = Field(..., description="Provider-specific voice ID for the speech.")
    model_id: Optional[str] = Field(
        None,
        description="Provider-specific model ID (e.g., 'sonic-2' for Cartesia, 'eleven_multilingual_v2' for ElevenLabs, 'gpt-4o-mini-tts' for OpenAI).",
    )

    output_format: Literal["mp3", "wav", "flac", "pcm_s16le", "pcm_mulaw"] = Field(
        "mp3",
        description="Desired audio output format. This will determine the Content-Type of the response.",
    )

    # Cartesia-specific parameters
    cartesia_language: Optional[str] = Field(
        "en",
        description="Language code for Cartesia TTS (e.g., 'en'). If None, Cartesia attempts auto-detection.",
    )
    cartesia_sample_rate: Optional[int] = Field(
        None,
        description="Optional sample rate for Cartesia (e.g., 24000, 44100). Provider defaults used if None.",
    )
    cartesia_bit_rate: Optional[int] = Field(
        None,
        description="Optional bit rate for Cartesia lossy formats like MP3 (e.g., 128000). Provider defaults used if None. Not for PCM.",
    )

    # ElevenLabs-specific parameters
    elevenlabs_optimize_streaming_latency: Optional[int] = Field(
        None,
        ge=0,
        le=4,
        description="0-4. Optimize for streaming latency for ElevenLabs.",
    )
    elevenlabs_voice_settings_stability: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="Stability for ElevenLabs voice settings.",
    )
    elevenlabs_voice_settings_similarity_boost: Optional[float] = Field(
        None,
        ge=0,
        le=1,
        description="Similarity boost for ElevenLabs voice settings.",
    )
    # If you need to specify the exact ElevenLabs output format string (e.g., "mp3_22050_32")
    # you could add a field like:
    # elevenlabs_explicit_output_format: Optional[str] = Field(None, description="Overrides output_format mapping for ElevenLabs if specified.")

    class Config:
        orm_mode = (
            True  # Though not directly mapping to ORM, good practice for consistency
        )
        schema_extra = {
            "example_cartesia": {
                "text": "Hello from Cartesia!",
                "provider": "cartesia",
                "voice_id": "694f9389-aac1-45b6-b726-9d9369183238",  # Example Cartesia Voice ID
                "model_id": "sonic-2",
                "output_format": "mp3",
                "cartesia_language": "en",
                "cartesia_sample_rate": 44100,
                "cartesia_bit_rate": 128000,
            },
            "example_elevenlabs": {
                "text": "Hello from ElevenLabs!",
                "provider": "elevenlabs",
                "voice_id": "JBFqnCBsd6RMkjVDRZzb",  # Example ElevenLabs Voice ID
                "model_id": "eleven_multilingual_v2",
                "output_format": "mp3",
                "elevenlabs_voice_settings_stability": 0.75,
                "elevenlabs_voice_settings_similarity_boost": 0.75,
            },
            "example_openai": {
                "text": "Hello from OpenAI!",
                "provider": "openai",
                "voice_id": "alloy",
                "model_id": "gpt-4o-mini-tts",
                "output_format": "mp3",
            },
        }


class VoiceDesignGeneratePreviewsRequest(BaseModel):
    voice_description: OptionalSafeText = Field(
        None,
        min_length=20,
        max_length=1000,
        description="Text prompt describing the desired voice characteristics (e.g., 'A deep, resonant male voice with a British accent, suitable for narration.'). If `bio` is provided, this field can be used to add more specific voice instructions. At least one of bio or voice_description should be provided.",
    )
    bio: OptionalSafeText = Field(
        None,
        description="A biography or background of the character to generate a voice description from. Used with `voice_description` to generate a richer prompt for the TTS provider. At least one of bio or voice_description should be provided.",
    )
    text: Optional[str] = Field(
        None,
        min_length=100,
        max_length=1000,
        description="Optional: Text to be spoken in the generated voice previews. If not provided, and auto_generate_text is false, ElevenLabs might use a default or generic text.",
    )
    auto_generate_text: Optional[bool] = Field(
        None,
        description="Optional: Whether to automatically generate a text suitable for the voice description if 'text' is not provided. Defaults to false by ElevenLabs.",
    )
    model_id: Optional[Literal["eleven_multilingual_ttv_v2", "eleven_ttv_v3"]] = Field(
        None,
        description="Optional: Model to use for voice generation.",
    )

    class Config:
        schema_extra = {
            "example": {
                "voice_description": "A warm, friendly female voice with a slight Southern American accent, perfect for an audiobook.",
                "text": "The quick brown fox jumps over the lazy dog. This is a sample text to hear how the designed voice sounds.",
                "auto_generate_text": False,
                "model_id": "eleven_multilingual_ttv_v2",
            },
            "example_with_bio": {
                "bio": "Ada Lovelace, born in 1815, was an English mathematician and writer, chiefly known for her work on Charles Babbage's proposed mechanical general-purpose computer, the Analytical Engine. She was the first to recognise that the machine had applications beyond pure calculation, and published the first algorithm intended to be carried out by such a machine.",
                "voice_description": "A clear, intelligent, and slightly formal British accent from the 19th century.",
                "text": "I am a mathematician, and a writer. I see the poetry in science.",
            },
        }


class VoiceDesignPreviewItem(BaseModel):
    audio_base_64: str = Field(
        ...,
        description="Base64 encoded audio sample of the generated voice preview.",
    )
    generated_voice_id: str = Field(
        ...,
        description="Temporary ID for this generated voice preview, used to create the full voice.",
    )
    media_type: str = Field(
        ...,
        description="MIME type of the audio sample, e.g., 'audio/mpeg'.",
    )
    duration_secs: Optional[float] = Field(
        None,
        description="Duration of the audio sample in seconds.",
    )


class VoiceDesignGeneratePreviewsAPIResponse(
    BaseModel,
):  # Maps to EL's successful response for /v1/text-to-voice/design
    previews: List[VoiceDesignPreviewItem]
    text: str  # The original voice_description text that was sent to EL


class VoiceDesignCreateFromPreviewRequest(BaseModel):
    generated_voice_id: str = Field(
        ...,
        description="The 'generated_voice_id' obtained from the '/design/preview'.",
    )
    voice_name: SafeLabel = Field(
        ...,
        description="Name for the new voice.",
    )
    voice_description: SafeText = Field(
        ...,
        description="Description for the new voice.",
    )
    audio_base_64: Optional[str] = Field(
        None,
        description="Base64 encoded audio sample from the selected voice preview. If provided, it's used for language detection.",
    )
    media_type: Optional[str] = Field(
        None,
        description="MIME type of the audio sample, e.g., 'audio/mpeg'. Assumed 'audio/mpeg' if sample is provided but this is omitted.",
    )
    labels: Optional[Dict[str, str]] = Field(
        None,
        description="Optional labels for ElevenLabs when creating the voice.",
    )
    language: Optional[str] = Field(
        None,
        description="Language of the voice. If not provided, it will be auto-detected from the provided audio preview, or from the description if no audio is provided.",
    )
    gender: Optional[str] = Field(
        None,
        description="Gender of the voice.",
    )

    class Config:
        schema_extra = {
            "example_with_audio": {
                "generated_voice_id": "temp_preview_id_from_step1",
                "voice_name": "My New Designed Voice",
                "voice_description": "A custom voice designed from text.",
                "audio_base_64": "UklGRiSAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQyAAAAA...",
                "media_type": "audio/mpeg",
                "gender": "male",
                "labels": {"use_case": "audiobook"},
            },
            "example_without_audio": {
                "generated_voice_id": "temp_preview_id_from_step1",
                "voice_name": "Another Designed Voice",
                "voice_description": "A deep, resonant voice for narration.",
                "gender": "male",
                "labels": {"use_case": "narration"},
            },
        }


class AssistantPhotoUploadResponse(BaseModel):
    gcs_url: str = Field(
        ...,
        description="GCS URL of the uploaded photo",
        example="gs://bucket/user_id/image_uuid.jpg",
    )


class AssistantVideoUploadResponse(BaseModel):
    gcs_url: str = Field(
        ...,
        description="GCS URL of the uploaded video",
        example="gs://bucket/user_id/video_uuid.mp4",
    )


class PhotoGenerateRequest(BaseModel):
    prompt: str = Field(..., description="Text prompt for image generation.")
    aspect_ratio: Optional[str] = Field(
        "1:1",
        description="Aspect ratio of the generated image.",
    )
    output_format: Optional[str] = Field(
        "webp",
        description="Format of the output image.",
    )
    output_quality: Optional[int] = Field(
        80,
        description="Quality of the output image (1-100).",
    )
    safety_tolerance: Optional[float] = Field(
        2.0,
        description="Safety tolerance for generation.",
    )
    prompt_upsampling: Optional[bool] = Field(
        True,
        description="Whether to use prompt upsampling.",
    )

    class Config:
        schema_extra = {
            "example": {
                "prompt": "A majestic lion in a field of lavender, photorealistic.",
                "aspect_ratio": "16:9",
            },
        }


class PhotoEditRequest(BaseModel):
    prompt: str = Field(..., description="Text prompt for editing the image.")
    input_image: HttpUrl = Field(..., description="URL of the input image to edit.")
    aspect_ratio: Optional[str] = Field(
        "match_input_image",
        description="Aspect ratio of the edited image.",
    )
    output_format: Optional[str] = Field(
        "jpg",
        description="Format of the output image.",
    )
    safety_tolerance: Optional[float] = Field(
        2.0,
        description="Safety tolerance for editing.",
    )

    class Config:
        schema_extra = {
            "example": {
                "prompt": "Make it look like an oil painting.",
                "input_image": "https://example.com/image.png",
            },
        }


class VideoAnimateRequest(BaseModel):
    """
    Schema for requesting video animation from an image and audio.
    File inputs (image_file, audio_file) are handled as Form/File in the endpoint.
    """

    image_url: Optional[HttpUrl] = Field(
        None,
        description="URL of the input portrait image.",
    )
    audio_url: Optional[HttpUrl] = Field(
        None,
        description="URL of the input audio file (WAV, MP3, etc.).",
    )
    seed: Optional[int] = Field(
        None,
        description="Random seed for reproducible results. Leave blank for a random seed.",
    )

    class Config:
        schema_extra = {
            "example": {
                "image_url": "https://raw.githubusercontent.com/jixiaozhong/Sonic/main/examples/image/anime1.png",
                "audio_url": "https://raw.githubusercontent.com/jixiaozhong/Sonic/main/examples/wav/talk_female_english_10s.MP3",
            },
        }


class ReplicatePredictionResponse(BaseModel):
    id: str
    model: str
    version: str
    input: Optional[Dict] = None
    output: Optional[Any] = None
    logs: Optional[str] = None
    error: Optional[Any] = None
    status: str
    created_at: str
    completed_at: Optional[str] = None
    urls: Optional[Dict] = None

    class Config:
        orm_mode = True
        from_attributes = True


class AssistantContactRemoval(BaseModel):
    """
    Schema for removing a contact method from an assistant.
    """

    contact_type: Literal["phone", "email", "whatsapp", "discord"] = Field(
        ...,
        description="The type of contact information to remove.",
        example="email",
    )


class AssistantContactCreate(BaseModel):
    """
    Schema for creating a new contact detail for an assistant.

    For ``provisioned_by="platform"`` (default) the endpoint provisions
    external infrastructure (Twilio phone, WhatsApp sender, Discord bot)
    and creates the corresponding AssistantContact row.

    Email contacts are **BYOD-only**: ``provisioned_by="user"`` plus a
    ``contact_value`` that the user connected via the OAuth flow.
    Platform-issued mailboxes (``provisioned_by="platform"`` with
    ``contact_type="email"``) are no longer offered and the endpoint
    returns HTTP 410 GONE for that combination.
    """

    contact_type: Literal["phone", "email", "whatsapp", "discord"] = Field(
        ...,
        description="The type of contact detail to create.",
        example="phone",
    )
    provisioned_by: Literal["platform", "user"] = Field(
        "platform",
        description=(
            "Who owns the resource. 'platform' = we provision and bill "
            "(phone / whatsapp / discord only); 'user' = BYOD, the user "
            "connected their own account via OAuth (required for email)."
        ),
    )
    # BYOD field — the full contact value (email address, phone number, etc.)
    # discovered from the OAuth provider after the user authenticates.
    contact_value: Optional[str] = Field(
        None,
        description=(
            "Full contact value for BYOD contacts (e.g. 'user@example.com'). "
            "Required when provisioned_by='user', ignored otherwise."
        ),
    )
    # Phone-specific fields
    phone_country: Optional[str] = Field(
        "US",
        description="Country code for phone number provisioning (e.g., 'US', 'GB'). Only used for phone contacts.",
        example="US",
    )
    # Email-specific fields (BYOD only — platform email is retired)
    email_provider: Optional[Literal["google_workspace", "microsoft_365"]] = Field(
        None,
        description=(
            "Provider of the user's connected mailbox. Required for BYOD "
            "email contacts; must be set explicitly to avoid mislabelling "
            "Microsoft mailboxes as Google Workspace."
        ),
        example="google_workspace",
    )

    @model_validator(mode="after")
    def _validate_byod_fields(self) -> "AssistantContactCreate":
        if self.provisioned_by == "user":
            if not self.contact_value:
                raise ValueError(
                    "contact_value is required when provisioned_by='user'.",
                )
            if self.contact_type == "email" and not self.email_provider:
                raise ValueError(
                    "email_provider is required when provisioned_by='user' "
                    "and contact_type='email'.",
                )
        return self

    class Config:
        schema_extra = {
            "example": {
                "contact_type": "phone",
                "phone_country": "US",
            },
        }


class AssistantContactRead(BaseModel):
    """
    Schema for reading an AssistantContact record with billing metadata.
    """

    id: int = Field(..., description="Unique identifier for the contact record.")
    assistant_id: int = Field(
        ...,
        description="ID of the assistant this contact belongs to.",
    )
    contact_type: Literal["phone", "email", "whatsapp", "discord"] = Field(
        ...,
        description="The type of contact detail.",
    )
    contact_value: str = Field(
        ...,
        description="The provisioned value (phone number, email address, etc.).",
    )
    provider: Optional[str] = Field(
        None,
        description="Provider used for provisioning (e.g., 'twilio', 'google_workspace').",
    )
    provisioned_by: str = Field(
        ...,
        description="Who provisioned this contact: 'platform' or 'user'.",
    )
    country_code: Optional[str] = Field(
        None,
        description="Country code for phone numbers.",
    )
    status: str = Field(
        ...,
        description="Lifecycle status: 'active', 'grace_period', or 'deleted'.",
    )
    monthly_cost: Optional[float] = Field(
        None,
        description="Monthly cost in dollars at time of last levy.",
    )
    created_at: datetime = Field(..., description="When this contact was created.")
    updated_at: Optional[datetime] = Field(
        None,
        description="When this contact was last updated.",
    )
    grace_period_started_at: Optional[datetime] = Field(
        None,
        description="When the grace period started (NULL if not in grace period).",
    )

    class Config:
        orm_mode = True
        from_attributes = True


class AssistantContactUpdate(BaseModel):
    """
    Schema for updating metadata on an existing contact.

    Only ``metadata`` can be changed via this endpoint. User-side contact
    info (phone, whatsapp) is managed on the user profile. Changing the
    actual provisioned resource requires delete + create.
    """

    contact_type: Literal["phone", "email", "whatsapp", "discord"] = Field(
        ...,
        description="The type of contact to update.",
        example="phone",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Updated type-specific metadata (merged with existing).",
    )

    class Config:
        schema_extra = {
            "example": {
                "contact_type": "phone",
                "metadata": {"custom_key": "value"},
            },
        }


class ConnectRequest(BaseModel):
    """Request body for ``POST /assistant/{id}/connect``."""

    provider: Literal["google", "microsoft"] = Field(
        ...,
        description="OAuth provider to connect.",
    )
    features: List[str] = Field(
        ["email", "teams"],
        description=(
            "Suite features to request access for (e.g. 'email', 'calendar', "
            "'drive').  Defaults to email+teams.  Features unavailable for "
            "the chosen provider are silently dropped (e.g. 'teams' on Google)."
        ),
    )
    redirect_after: Optional[str] = Field(
        None,
        description="URL to redirect the user to after OAuth completes.",
    )

    @model_validator(mode="after")
    def _validate_and_filter_features(self) -> "ConnectRequest":
        from orchestra.web.api.assistant.scopes import (
            REQUIRED_FEATURES,
            available_features,
        )

        valid_for_provider = set(available_features(self.provider))
        all_known = set(available_features("google")) | set(
            available_features("microsoft"),
        )
        truly_unknown = set(self.features) - all_known
        if truly_unknown:
            raise ValueError(
                f"Unknown features: {sorted(truly_unknown)}. "
                f"Valid: {sorted(all_known)}",
            )
        self.features = [f for f in self.features if f in valid_for_provider]

        for feat in REQUIRED_FEATURES.get(self.provider, []):
            if feat not in self.features:
                self.features.append(feat)

        return self


class ConnectResponse(BaseModel):
    """Response from the connect endpoint with the OAuth authorization URL."""

    oauth_url: str = Field(
        ...,
        description="OAuth authorization URL the user should visit to grant access.",
    )


class GrantedFeaturesResponse(BaseModel):
    """Response from the granted-features endpoint."""

    provider: Optional[str] = Field(
        None,
        description="The connected OAuth provider ('google' or 'microsoft'), or null if none.",
    )
    features: List[str] = Field(
        [],
        description="Feature names whose scopes are fully granted.",
    )
    required_features: List[str] = Field(
        [],
        description="Features that cannot be removed (Console should grey these out).",
    )


class WorkspaceFileNode(BaseModel):
    """A single Drive / SharePoint / OneDrive item in a browse listing."""

    drive_id: str = Field(..., description="Provider drive/corpus id.")
    item_id: str = Field(..., description="Provider item id (folder or file).")
    name: str = Field("", description="Display name.")
    kind: Literal["drive", "folder", "file"] = Field(
        "file",
        description="Node kind: a drive root, a folder, or a leaf file.",
    )
    mime_type: Optional[str] = Field(None, description="MIME type, when known.")
    web_url: Optional[str] = Field(None, description="Provider web URL, when known.")
    parent_id: Optional[str] = Field(
        None,
        description="Parent item id within the same drive, when known.",
    )


class WorkspaceFileListResponse(BaseModel):
    """Listing of workspace file nodes (roots, children, or search hits)."""

    items: List[WorkspaceFileNode] = Field([], description="The listed nodes.")


class WorkspaceFileDecision(BaseModel):
    """An explicit allow/deny decision for one Drive/SharePoint item."""

    drive_id: str = Field(..., description="Provider drive/corpus id.")
    item_id: str = Field(..., description="Provider item id.")
    allow: bool = Field(..., description="Whether access is allowed.")
    kind: Literal["drive", "folder", "file"] = Field("folder", description="Node kind.")
    name: str = Field("", description="Display name captured at selection time.")
    path: str = Field("", description="Human-readable path captured at selection time.")


class WorkspaceFilePolicy(BaseModel):
    """The full file-access allowlist for one provider."""

    provider: Literal["google", "microsoft"] = Field(..., description="OAuth provider.")
    default_allow: bool = Field(
        False,
        description="Whether items without an explicit decision are accessible "
        "(governs newly-added files at undecided locations).",
    )
    decisions: List[WorkspaceFileDecision] = Field(
        [],
        description="Explicit per-item allow/deny overrides.",
    )


class WorkspaceFilePolicyUpdate(BaseModel):
    """Request body for ``PATCH /assistant/{id}/workspace-files/policy``."""

    default_allow: bool = Field(
        False,
        description="Default access for items without an explicit decision.",
    )
    decisions: List[WorkspaceFileDecision] = Field(
        [],
        description="Explicit per-item allow/deny overrides.",
    )


class WorkspaceFileAccessAdminResponse(BaseModel):
    """Admin read of every provider's file-access policy for an assistant.

    Consumed by the assistant runtime (Droid) to mirror the allowlist into its
    enforcement layer.
    """

    policies: List[WorkspaceFilePolicy] = Field(
        [],
        description="Configured per-provider allowlists (absent providers omitted).",
    )


class SecretCreate(BaseModel):
    """Request body for ``POST /assistant/{id}/secret``."""

    secret_name: SafeLabel = Field(..., description="Unique name for the secret.")
    secret_value: str = Field(..., description="Secret payload (token, key, etc.).")


class SecretUpdate(BaseModel):
    """Request body for ``PUT /assistant/{id}/secret/{name}``."""

    secret_value: str = Field(..., description="New value for the secret.")


class AssistantTransferToOrgRequest(BaseModel):
    """
    Schema for transferring an assistant from personal to organizational workspace.
    """

    organization_id: int = Field(
        ...,
        description="Target organization ID to transfer the assistant to.",
        example=123,
    )
    transfer_logs: bool = Field(
        True,
        description="Whether to transfer existing logs from personal 'Assistants' project to org 'Assistants' project.",
    )

    class Config:
        schema_extra = {
            "example": {
                "organization_id": 123,
                "transfer_logs": True,
            },
        }


class AssistantTransferToPersonalRequest(BaseModel):
    """
    Schema for transferring an assistant from organizational to personal workspace.
    """

    delete_logs: bool = Field(
        True,
        description="Whether to delete related logs from the org 'Assistants' project.",
    )

    class Config:
        schema_extra = {
            "example": {
                "delete_logs": True,
            },
        }


class AssistantTransferResponse(BaseModel):
    """
    Response schema for assistant transfer operations.
    """

    message: str = Field(
        ...,
        description="Success message describing the transfer result.",
    )
    agent_id: int = Field(
        ...,
        description="ID of the transferred assistant.",
    )
    transferred_from: str = Field(
        ...,
        description="Source workspace type ('personal' or 'organization').",
    )
    transferred_to: str = Field(
        ...,
        description="Target workspace type ('personal' or 'organization').",
    )
    logs_transferred: Optional[bool] = Field(
        None,
        description="Whether logs were transferred (only for personal->org transfers).",
    )
    logs_deleted: Optional[bool] = Field(
        None,
        description="Whether logs were deleted (only for org->personal transfers).",
    )


# Admin schemas


class AdminUpdateUserByAssistant(BaseModel):
    """
    Admin schema for updating a user's profile via assistant lookup.

    For personal assistants: updates the owner's profile.
    For org assistants: finds the member by email and updates their profile.
    """

    assistant_id: int = Field(
        ...,
        description="The ID of the assistant to use for user lookup.",
    )
    target_user_email: str = Field(
        ...,
        description="Email of the target user to update. Must match the assistant owner (personal) or an org member (organizational).",
    )
    timezone: Optional[str] = Field(
        None,
        description="Timezone to set for the user in IANA format.",
        example="America/New_York",
    )
    bio: OptionalSafeText = Field(
        None,
        description="Bio/description to set for the user.",
        example="Software engineer focused on AI systems.",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v


class AdminUpdateUserByAssistantResponse(BaseModel):
    """Response schema for admin update user by assistant."""

    info: str = Field(..., description="Success message.")
    user_id: str = Field(..., description="ID of the updated user.")
    email: str = Field(..., description="Email of the updated user.")
    assistant_type: str = Field(
        ...,
        description="Type of assistant ('personal' or 'organization').",
    )


class ContactMembershipCreate(BaseModel):
    """Admin request for an assistant contact relationship overlay."""

    contact_id: int = Field(..., description="Contact row id within the target root.")
    target_scope: Literal["personal", "team"] = Field(
        ...,
        description="Whether the contact id points at personal contacts or a team root.",
    )
    target_team_id: Optional[int] = Field(
        None,
        description="Team id when target_scope is 'team'.",
    )
    relationship: Literal["self", "boss", "coworker", "other"] = Field(
        ...,
        description="Assistant-specific relationship to the contact.",
    )
    should_respond: bool = Field(
        True,
        description="Whether the assistant should respond to this contact.",
    )
    response_policy: str = Field(
        "standard",
        description="Policy text or slug used by the runtime when responding.",
    )
    can_edit: bool = Field(
        False,
        description="Whether the assistant can edit the contact's shared facts.",
    )

    @model_validator(mode="after")
    def validate_target_polarity(self) -> "ContactMembershipCreate":
        if self.target_scope == "personal" and self.target_team_id is not None:
            raise ValueError(
                "personal contact memberships cannot include target_team_id",
            )
        if self.target_scope == "team" and self.target_team_id is None:
            raise ValueError("team contact memberships require target_team_id")
        return self


class ContactMembershipRead(BaseModel):
    """Admin response shape for an assistant contact relationship overlay."""

    id: int
    assistant_id: int
    authoring_assistant_id: Optional[int]
    contact_id: int
    target_scope: str
    target_team_id: Optional[int]
    relationship: str
    should_respond: bool
    response_policy: str
    can_edit: bool
    created_at: datetime


class ContactMembershipUpsertResponse(BaseModel):
    """Admin response for idempotent contact-membership creation."""

    membership: ContactMembershipRead
    created: bool


class ContactMembershipDeleteResponse(BaseModel):
    """Admin response for deleting contact relationship overlays."""

    deleted: int


class AdminUpdateAssistant(BaseModel):
    """
    Admin schema for updating assistant details directly.
    Bypasses permission checks for admin operations.
    """

    timezone: Optional[str] = Field(
        None,
        description="Timezone to set for the assistant in IANA format.",
        example="Europe/London",
    )
    about: OptionalSafeText = Field(
        None,
        description="About/description to set for the assistant.",
        example="AI assistant specializing in customer support.",
    )
    job_title: Optional[str] = Field(
        None,
        description=(
            "Free-text job title / specialization for the assistant "
            "(e.g. 'Growth marketing'). Whitespace is trimmed; pass an "
            "empty string to clear."
        ),
        example="Growth marketing",
        max_length=120,
    )
    desktop_filesync_sshkey: Optional[str] = Field(
        None,
        description="SSH private key for desktop filesystem sync.",
    )
    console_config: Optional[Dict[str, Any]] = Field(
        None,
        description="Per-assistant UI/UX configuration (layout, tabs, theme). "
        "Pass the full nested object to upsert; pass null to clear; omit to leave unchanged.",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v

    @field_validator("job_title")
    @classmethod
    def normalize_admin_job_title(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_job_title(v)


class AdminUpdateAssistantResponse(BaseModel):
    """Response schema for admin update assistant."""

    info: str = Field(..., description="Success message.")
    assistant_id: int = Field(..., description="ID of the updated assistant.")
    updated_fields: List[str] = Field(
        ...,
        description="List of fields that were updated.",
    )


class Contact(BaseModel):
    """Contact schema for admin_list_contacts endpoint."""

    user_id: Optional[str] = None
    first_name: Optional[str] = None
    surname: Optional[str] = None
    email_address: Optional[str] = None
    phone_number: Optional[str] = None
    whatsapp_number: Optional[str] = None
    description: Optional[str] = None
    custom_fields: Dict[str, Any] = {}


# ============================================================================
# Spending Limit Schemas
# ============================================================================


class SpendingLimitRequest(BaseModel):
    """Request body for setting a spending limit."""

    monthly_spending_cap: Optional[float] = Field(
        ...,
        description="Monthly spending limit in dollars. Set to null to remove the limit.",
        example=100.00,
        ge=0,
    )


class AssistantSpendResponse(BaseModel):
    """Response for getting assistant monthly spend."""

    agent_id: int = Field(..., description="Assistant ID.")
    month: str = Field(..., description="Month in YYYY-MM format.")
    cumulative_spend: float = Field(
        ...,
        description="Total spend for this assistant in the specified month.",
        example=78.50,
    )
    limit: Optional[float] = Field(
        None,
        description="Monthly spending limit for this assistant.",
        example=100.00,
    )
    limit_set_at: Optional[datetime] = Field(
        None,
        description="When the spending limit was last changed.",
        example="2026-02-01T10:00:00Z",
    )
    percent_used: Optional[float] = Field(
        None,
        description="Percentage of limit used (null if no limit set).",
        example=78.5,
    )
    credit_balance: Optional[float] = Field(
        None,
        description="Current credit balance of the billing account.",
    )
    billing_mode: str = Field(
        "CREDITS",
        description=(
            "Billing mode of the assistant's billing account: CREDITS "
            "(pre-paid wallet) or METERED (invoiced monthly)."
        ),
    )


class AssistantSpendingLimitResponse(BaseModel):
    """Response for setting assistant spending limit."""

    agent_id: int = Field(..., description="Assistant ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The set monthly spending limit.",
        example=100.00,
    )
    effective_limit: Optional[float] = Field(
        None,
        description="Effective limit (may be lower due to user/org limit).",
        example=100.00,
    )


class UserSpendingLimitResponse(BaseModel):
    """Response for setting user spending limit."""

    user_id: str = Field(..., description="User ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The set monthly spending limit.",
        example=200.00,
    )
    effective_limit: Optional[float] = Field(
        None,
        description="Effective limit (may be lower due to org limit).",
        example=200.00,
    )
    cascaded_updates: Optional[Dict[str, int]] = Field(
        None,
        description="Count of child entities that had their limits capped.",
        example={"assistants_capped": 3},
    )


class OrgSpendingLimitResponse(BaseModel):
    """Response for setting organization spending limit."""

    organization_id: int = Field(..., description="Organization ID.")
    monthly_spending_cap: Optional[float] = Field(
        None,
        description="The set monthly spending limit.",
        example=500.00,
    )
    cascaded_updates: Optional[Dict[str, int]] = Field(
        None,
        description="Count of child entities that had their limits capped.",
        example={"users_capped": 3, "assistants_capped": 7},
    )


# ============================================================================
# Spending Limit Notification Schemas
# ============================================================================


class SpendingLimitReachedRequest(BaseModel):
    """Request body for notifying that a spending limit was reached."""

    limit_type: Literal["assistant", "user", "member", "organization"] = Field(
        ...,
        description="Type of limit that was reached.",
        example="assistant",
    )
    entity_id: str = Field(
        ...,
        description="ID of the entity whose limit was reached.",
        example="123",
    )
    limit_value: float = Field(
        ...,
        description="The limit value that was reached.",
        example=100.00,
        ge=0,
    )
    current_spend: float = Field(
        ...,
        description="Current spend amount.",
        example=100.50,
        ge=0,
    )
    month: str = Field(
        ...,
        description="Billing month in YYYY-MM format.",
        example="2026-02",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    )
    limit_set_at: Optional[datetime] = Field(
        None,
        description="When the limit was last configured (for re-enable detection).",
        example="2026-02-01T10:00:00Z",
    )
    entity_name: Optional[str] = Field(
        None,
        description="Name of the entity (for email content).",
        example="Ada Lovelace",
    )
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID (required for member limits, entity_id is the user_id).",
        example=123,
    )


class SpendingLimitReachedResponse(BaseModel):
    """Response for spending limit notification endpoint."""

    notified: bool = Field(
        ...,
        description="Whether a notification was sent.",
        example=True,
    )
    reason: Optional[str] = Field(
        None,
        description="Reason for skipping notification (if notified=False).",
        example="already_notified",
    )
    recipient_count: Optional[int] = Field(
        None,
        description="Number of users who received the notification.",
        example=1,
    )
    notified_user_ids: Optional[List[str]] = Field(
        None,
        description="List of user IDs who received the notification.",
        example=["user_abc123"],
    )
