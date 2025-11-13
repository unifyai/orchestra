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
    user_local_desktop: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="User's local desktop operating system",
        example="windows",
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
    phone: Optional[str] = Field(
        None,
        description="Phone number of the assistant (just for testing purposes)",
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
                "user_local_desktop": "windows",
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
        description="ID of the user to associate the assistant with",
        example="123",
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
        description="API key of the assistant",
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
                "user_local_desktop": "windows",
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
                "created_at": "2025-04-25T10:30:00Z",
                "updated_at": "2025-04-26T14:15:00Z",
                "api_key": "1234567890",
                "user_first_name": "Ada",
                "user_last_name": "Lovelace",
                "user_email": "ada.lovelace@unify.ai",
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
    user_local_desktop: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="User's local desktop operating system",
        example="macos",
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
                "user_local_desktop": "macos",
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
    dynamic_scale: Optional[float] = Field(
        1.0,
        description="Controls movement intensity. Increase/decrease for more/less movement.",
        ge=0.5,
        le=2.0,
    )
    min_resolution: Optional[int] = Field(
        512,
        description="Minimum image resolution for processing. Lower values use less memory but may reduce quality.",
        ge=256,
        le=1024,
    )
    inference_steps: Optional[int] = Field(
        25,
        description="Number of diffusion steps. Higher values may improve quality but take longer.",
        ge=5,
        le=50,
    )
    keep_resolution: Optional[bool] = Field(
        True,
        description="If true, output video matches the original image resolution. Otherwise uses the min_resolution after cropping.",
    )

    class Config:
        schema_extra = {
            "example": {
                "image_url": "https://raw.githubusercontent.com/jixiaozhong/Sonic/main/examples/image/anime1.png",
                "audio_url": "https://raw.githubusercontent.com/jixiaozhong/Sonic/main/examples/wav/talk_female_english_10s.MP3",
                "dynamic_scale": 1.2,
                "keep_resolution": True,
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
