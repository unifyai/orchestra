from datetime import datetime
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar
from zoneinfo import available_timezones

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from pydantic.generics import GenericModel

T = TypeVar("T")

VALID_TIMEZONES = available_timezones()


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
    """

    first_name: Optional[str] = Field(
        None,
        description="First name of the assistant",
        example="Ada",
    )
    surname: Optional[str] = Field(
        None,
        description="Surname of the assistant",
        example="Lovelace",
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
    desktop_url: Optional[str] = Field(
        None,
        description="URL to the assistant's desktop profile/page",
        example="https://app.example.com/assistants/ada",
    )
    desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Desktop operating system mode for assistant's VM creation",
        example="windows",
    )
    user_desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Operating system mode of the user's own desktop",
        example="macos",
    )
    user_desktop_filesys_sync: Optional[bool] = Field(
        False,
        description="Whether to enable filesystem sync with user's desktop",
        example=False,
    )
    user_desktop_url: Optional[str] = Field(
        None,
        description="URL for communication with user's desktop",
        example="https://user-desktop.example.com",
    )
    about: Optional[str] = Field(
        None,
        description="Brief description about the assistant",
        example="Mathematician and writer known for work on Analytical Engine",
    )
    email: Optional[str] = Field(
        None,
        description="Email of the assistant",
        example="ada.lovelace@unify.ai",
    )
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
    voice_mode: Optional[Literal["tts", "sts"]] = Field(
        None,
        description="The type of voice interaction, either text-to-speech (tts) or speech-to-speech (sts).",
        example="tts",
    )
    user_phone: Optional[str] = Field(
        None,
        description="Contact phone number of the user",
        example="+15551234567",
    )
    user_whatsapp_number: Optional[str] = Field(
        None,
        description="WhatsApp number of the user",
        example="+15551234567",
    )
    create_infra: Optional[bool] = Field(
        True,
        description="Whether to create the infrastructure for the assistant",
        exclude=True,
    )
    phone_country: Optional[str] = Field(
        "US",
        description="Country code for phone number provisioning (e.g., US, GB)",
        example="US",
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

    @model_validator(mode="after")
    def check_voice_fields(cls, self):
        voice_id, voice_provider, voice_mode = (
            self.voice_id,
            self.voice_provider,
            self.voice_mode,
        )

        # If any voice field is provided, id and provider are required.
        if any(v is not None for v in [voice_id, voice_provider, voice_mode]):
            if voice_id is None or voice_provider is None:
                raise ValueError(
                    "If providing voice information, both 'voice_id' and 'voice_provider' are required.",
                )
            # Default voice_mode if it wasn't specified
            if voice_mode is None:
                self.voice_mode = "tts"
        return self

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "nationality": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada.mp4",
                "desktop_url": "https://app.example.com/assistants/ada",
                "desktop_mode": "windows",
                "user_desktop_mode": "macos",
                "user_desktop_filesys_sync": False,
                "user_desktop_url": "https://user-desktop.example.com",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "phone_country": "US",
                "timezone": "America/New_York",
                "email": "ada.lovelace@unify.ai",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "voice_provider": "cartesia",
                "voice_mode": "tts",
                "user_phone": "+15551234567",
                "user_whatsapp_number": "+15551234567",
            },
        }


class AssistantRead(AssistantCreate):
    """
    Schema for reading assistant data, extends AssistantCreate with additional fields.
    """

    agent_id: str = Field(
        ...,
        description="Unique identifier for the assistant",
        example="12345",
    )
    user_id: str = Field(
        ...,
        description="ID of the user who created/owns the assistant",
        example="123",
    )
    organization_id: Optional[int] = Field(
        None,
        description="Organization ID if this is an organizational assistant, None for personal assistants",
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
    phone: Optional[str] = Field(
        None,
        description="Phone number of the assistant",
        example="+15551234567",
    )
    assistant_whatsapp_number: Optional[str] = Field(
        None,
        description="WhatsApp number of the assistant",
        example="+15551234567",
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
    secrets: Optional[Dict[str, str]] = Field(
        None,
        description="Dictionary of secret names to values. Only returned via admin endpoints.",
        example={"openai_api_key": "sk-..."},
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

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "nationality": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada.mp4",
                "desktop_url": "https://app.example.com/assistants/ada",
                "desktop_mode": "windows",
                "user_desktop_mode": "macos",
                "user_desktop_filesys_sync": False,
                "user_desktop_url": "https://user-desktop.example.com",
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
                "voice_mode": "tts",
                "agent_id": "12345",
                "user_id": "123",
                "organization_id": None,
                "created_at": "2025-04-25T10:30:00Z",
                "updated_at": "2025-04-26T14:15:00Z",
                "api_key": "1234567890",
                "user_first_name": "Ada",
                "user_last_name": "Lovelace",
                "user_email": "ada.lovelace@unify.ai",
                "secrets": {"openai_api_key": "sk-..."},
            },
        }


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
    label: str = Field(
        ...,
        description="Human-readable label for this demo (e.g., 'Richard Branson demo')",
        example="Richard Branson demo",
    )
    first_name: str = Field(
        ...,
        description="First name of the demo assistant",
        example="Lucy",
    )
    surname: str = Field(
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

    class Config:
        schema_extra = {
            "example": {
                "source_assistant_id": 12345,
                "label": "Richard Branson demo",
                "first_name": "Lucy",
                "surname": "Branson-Demo",
                "demoer_phone": "+14155559999",
                "monthly_spending_cap": 10.0,
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

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "id": 42,
                "source_assistant_id": 12345,
                "demoer_user_id": "user_abc123",
                "label": "Richard Branson demo",
                "created_at": "2026-02-10T14:30:00Z",
            },
        }


class AssistantUpdate(BaseModel):
    """
    Schema for updating an existing assistant.
    Only includes fields that can be updated.
    """

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
    desktop_url: Optional[str] = Field(
        None,
        description="URL to the assistant's desktop profile/page",
        example="https://app.example.com/assistants/ada",
    )
    desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Desktop operating system mode for VM creation",
        example="macos",
    )
    user_desktop_mode: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Operating system mode of the user's own desktop",
        example="macos",
    )
    user_desktop_filesys_sync: Optional[bool] = Field(
        None,
        description="Whether to enable filesystem sync with user's desktop",
        example=False,
    )
    user_desktop_url: Optional[str] = Field(
        None,
        description="URL for communication with user's desktop",
        example="https://user-desktop.example.com",
    )
    about: Optional[str] = Field(
        None,
        description="Brief description about the assistant",
        example="Award-winning mathematician specializing in algorithm development",
    )
    user_phone: Optional[str] = Field(
        None,
        description="Contact phone number of the user",
        example="+15551234567",
    )
    phone: Optional[str] = Field(
        None,
        description="Contact phone number for the assistant",
        example="+15559876543",
    )
    phone_country: Optional[str] = Field(
        None,
        description="Country code for phone number provisioning (e.g., US, GB)",
        example="GB",
    )
    email: Optional[str] = Field(
        None,
        description="Email address for the assistant",
        example="ada.lovelace@newdomain.com",
    )
    user_whatsapp_number: Optional[str] = Field(
        None,
        description="WhatsApp number of the user",
        example="+15559876543",
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
    voice_mode: Optional[Literal["tts", "sts"]] = Field(
        None,
        description="The type of voice interaction, either text-to-speech (tts) or speech-to-speech (sts).",
        example="tts",
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

    @model_validator(mode="after")
    def check_voice_fields_on_update(cls, self):
        """Validate voice fields for PATCH operations."""
        provided = self.__pydantic_fields_set__

        has_id = "voice_id" in provided
        has_provider = "voice_provider" in provided
        has_mode = "voice_mode" in provided

        # No voice fields provided, nothing to do
        if not any([has_id, has_provider, has_mode]):
            return self

        # Clearing voice by sending "voice_id": null
        if has_id and self.voice_id is None:
            self.voice_provider = None
            self.voice_mode = None
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

            # Default voice_mode if not provided
            if not has_mode:
                self.voice_mode = "tts"

        # Only mode was provided, which is not allowed.
        elif has_mode:
            raise ValueError(
                "Cannot update 'voice_mode' alone. Please provide 'voice_id' and 'voice_provider'.",
            )

        return self

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "weekly_limit": 20.5,
                "max_parallel": 3,
                "profile_photo": "https://example.com/photos/ada.jpg",
                "profile_video": "https://example.com/videos/ada_new.mp4",
                "desktop_url": "https://app.example.com/assistants/ada",
                "desktop_mode": "macos",
                "user_desktop_mode": "macos",
                "user_desktop_filesys_sync": True,
                "user_desktop_url": "https://user-desktop.example.com",
                "about": "Award-winning mathematician specializing in algorithm development",
                "user_phone": "+15551234567",
                "phone": "+15559876543",
                "user_whatsapp_number": "+15559876543",
                "assistant_whatsapp_number": "+15559876543",
                "email": "ada.lovelace@newdomain.com",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "voice_provider": "cartesia",
                "voice_mode": "tts",
                "phone_country": "GB",
                "timezone": "Europe/London",
            },
        }


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


class RecordingCreate(BaseModel):
    user_id: str = Field(
        ...,
        description="ID of the user to associate the recording with",
        example="123",
    )
    assistant_id: int = Field(
        ...,
        description="ID of the assistant to associate the recording with",
        example=123,
    )
    conference_name: str = (
        Field(
            ...,
            description="Name of the conference to associate the recording with",
            example="Unity_Sample_Conference",
        ),
    )
    recording_raw: str = Field(
        ...,
        description="Base64-encoded audio payload",
        example="UklGRiSAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQyAAAAA...",
    )
    content_type: Optional[str] = Field(
        None,
        description="Content type of the audio file",
        example="audio/wav",
    )

    class Config:
        schema_extra = {
            "example": {
                "recording_raw": "UklGRiSAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQyAAAAA...",
                "content_type": "audio/wav",
            },
        }


class RecordingInfo(BaseModel):
    id: int
    url: HttpUrl
    created_at: datetime

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "id": 123,
                "url": "https://storage.example.com/recordings/call_123.wav",
                "created_at": "2025-05-08T14:30:00Z",
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
    name: str = Field(
        ...,
        description="User-given name for the voice",
        example="English Woman Calm 1",
    )
    description: str = Field(
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
    name: str = Field(..., description="Name for the new cloned voice")
    language: str = Field(..., description="Language of the audio clip (e.g., 'en')")
    description: Optional[str] = Field(
        None,
        description="Optional description for the voice",
    )


class VoiceGenerateRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize.")
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
    voice_description: Optional[str] = Field(
        None,
        min_length=20,
        max_length=1000,
        description="Text prompt describing the desired voice characteristics (e.g., 'A deep, resonant male voice with a British accent, suitable for narration.'). If `bio` is provided, this field can be used to add more specific voice instructions. At least one of bio or voice_description should be provided.",
    )
    bio: Optional[str] = Field(
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
    voice_name: str = Field(
        ...,
        description="Name for the new voice.",
    )
    voice_description: str = Field(
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
        example="gs://your-bucket-name/user_id/image_uuid.jpg",
    )


class AssistantVideoUploadResponse(BaseModel):
    gcs_url: str = Field(
        ...,
        description="GCS URL of the uploaded video",
        example="gs://your-bucket-name/user_id/video_uuid.mp4",
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
    duration: Optional[int] = Field(
        None,
        description="Duration of the generated video. Defaults to 5 sec.",
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

    contact_type: Literal["phone", "email", "whatsapp"] = Field(
        ...,
        description="The type of contact information to remove.",
        example="email",
    )


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


# Assistant Secrets


class SecretCreate(BaseModel):
    """
    Schema for creating or updating an assistant secret.
    """

    secret_name: str = Field(
        ...,
        description="Unique name/key for the secret (e.g., 'openai_api_key', 'github_token').",
        example="openai_api_key",
        min_length=1,
        max_length=255,
    )
    secret_value: str = Field(
        ...,
        description="The secret value (API key, token, etc.).",
        example="sk-...",
        min_length=1,
    )
    description: Optional[str] = Field(
        None,
        description="Optional description of what this secret is used for.",
        example="OpenAI API key for GPT-4 access",
    )

    class Config:
        schema_extra = {
            "example": {
                "secret_name": "openai_api_key",
                "secret_value": "sk-...",
                "description": "OpenAI API key for GPT-4 access",
            },
        }


class SecretUpdate(BaseModel):
    """
    Schema for updating an existing secret.
    """

    secret_value: Optional[str] = Field(
        None,
        description="New secret value.",
        example="sk-new-...",
        min_length=1,
    )
    description: Optional[str] = Field(
        None,
        description="New description for the secret.",
        example="Updated OpenAI API key",
    )

    class Config:
        schema_extra = {
            "example": {
                "secret_value": "sk-new-...",
                "description": "Updated OpenAI API key",
            },
        }


class SecretRead(BaseModel):
    """
    Schema for reading secret metadata (value is masked).
    """

    secret_name: str = Field(
        ...,
        description="The name/key of the secret.",
        example="openai_api_key",
    )
    description: Optional[str] = Field(
        None,
        description="Description of what this secret is used for.",
        example="OpenAI API key for GPT-4 access",
    )
    created_at: Optional[datetime] = Field(
        None,
        description="When the secret was created.",
    )
    updated_at: Optional[datetime] = Field(
        None,
        description="When the secret was last updated.",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "secret_name": "openai_api_key",
                "description": "OpenAI API key for GPT-4 access",
                "created_at": "2025-01-15T10:30:00Z",
                "updated_at": "2025-01-15T10:30:00Z",
            },
        }


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
    bio: Optional[str] = Field(
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
    about: Optional[str] = Field(
        None,
        description="About/description to set for the assistant.",
        example="AI assistant specializing in customer support.",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_TIMEZONES:
            raise ValueError(f"'{v}' is not a valid IANA timezone.")
        return v


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
