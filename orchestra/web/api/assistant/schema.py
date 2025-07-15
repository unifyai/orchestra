from datetime import datetime
from typing import Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, Field, HttpUrl, root_validator
from pydantic.generics import GenericModel

T = TypeVar("T")


class InfoResponse(GenericModel, Generic[T]):
    """
    Generic wrapper for API responses.
    Wraps any response type under an 'info' key while preserving schema validation.
    """

    info: T


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
    region: Optional[str] = Field(
        None,
        description="Geographic region of the assistant",
        example="North America",
    )
    profile_photo: Optional[str] = Field(
        None,
        description="URL to the assistant's profile photo",
        example="https://example.com/photos/ada.jpg",
    )
    about: Optional[str] = Field(
        None,
        description="Brief description about the assistant",
        example="Mathematician and writer known for work on Analytical Engine",
    )
    country: Optional[str] = Field(
        "US",
        description="Country code for phone number provisioning (e.g., US, GB)",
        example="US",
    )
    email: Optional[str] = Field(
        None,
        description="Email of the assistant",
        example="ada.lovelace@unify.ai",
    )
    voice_id: Optional[str] = Field(  # This is Cartesia's voice ID
        None,
        description="Id of the voice (Cartesia ID) to use for the assistant",
        example="bf0a246a-8642-498a-9950-80c35e9276b5",
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

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "region": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "country": "US",
                "email": "ada.lovelace@unify.ai",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
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
    tts_provider: Optional[str] = Field(
        "cartesia",
        description="TTS provider of the assistant",
        example="cartesia",
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
                "region": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "country": "US",
                "email": "ada.lovelace@unify.ai",
                "phone": "+15551234567",
                "user_phone": "+15551234567",
                "user_whatsapp_number": "+15551234567",
                "assistant_whatsapp_number": "+15551234567",
                "tts_provider": "cartesia",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "agent_id": "12345",
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
    country: Optional[str] = Field(
        None,
        description="Country code for phone number provisioning (e.g., US, GB)",
        example="GB",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "weekly_limit": 20.5,
                "max_parallel": 3,
                "about": "Award-winning mathematician specializing in algorithm development",
                "user_phone": "+15551234567",
                "phone": "+15559876543",
                "user_whatsapp_number": "+15559876543",
                "assistant_whatsapp_number": "+15559876543",
                "email": "ada.lovelace@newdomain.com",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "country": "GB",
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
    uptime_seconds: float = Field(..., description="Service uptime in seconds.")
    process_id: Optional[int] = Field(
        None,
        description="The process ID of the assistant service.",
    )
    assistant_id: str = Field(
        ...,
        description="The ID of the assistant, as configured in its environment.",
    )
    shutdown_reason: Optional[str] = Field(
        None,
        description="The reason for the last shutdown, if applicable.",
    )
    inactivity_timeout_minutes: int = Field(
        ...,
        description="The configured inactivity timeout in minutes.",
    )
    message: Optional[str] = Field(
        None,
        description="An additional human-readable status message.",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example_running": {
                "running": True,
                "uptime_seconds": 3600.5,
                "process_id": 12345,
                "assistant_id": "123",
                "shutdown_reason": None,
                "inactivity_timeout_minutes": 6,
                "message": None,
            },
            "example_inactive": {
                "running": False,
                "uptime_seconds": 0,
                "process_id": None,
                "assistant_id": "123",
                "shutdown_reason": "inactivity_timeout",
                "inactivity_timeout_minutes": 6,
                "message": "Service shut down due to 6 minutes of inactivity",
            },
        }


class RecordingCreate(BaseModel):
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
    The voice_id is provided by Cartesia after a successful clone/localize, or is a known preset ID.
    """

    voice_id: str = Field(
        ...,
        description="Cartesia Voice ID",
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
    provider: Literal["cartesia", "elevenlabs"] = Field(
        "cartesia",
        description="Provider of the voice (cartesia or elevenlabs)",
        example="cartesia",
    )
    is_preset: Optional[bool] = Field(
        False,
        description="Whether this voice is a Cartesia preset or user-created.",
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
    provider: Literal["cartesia", "elevenlabs"] = Field(
        ...,
        description="TTS provider.",
    )
    voice_id: str = Field(..., description="Provider-specific voice ID for the speech.")
    model_id: Optional[str] = Field(
        None,
        description="Provider-specific model ID (e.g., 'sonic-2' for Cartesia, 'eleven_multilingual_v2' for ElevenLabs).",
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
        }


class VoiceDesignGeneratePreviewsRequest(BaseModel):
    voice_description: Optional[str] = Field(
        None,
        min_length=20,
        max_length=1000,
        description="Text prompt describing the desired voice characteristics (e.g., 'A deep, resonant male voice with a British accent, suitable for narration.'). If `bio` is provided, this field can be used to add more specific voice instructions.",
    )
    bio: Optional[str] = Field(
        None,
        description="A biography or background of the character to generate a voice description from. Used with `voice_description` to generate a richer prompt for the TTS provider.",
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

    @root_validator
    def check_description_or_bio(cls, values):
        if not values.get("voice_description") and not values.get("bio"):
            raise ValueError("Either 'voice_description' or 'bio' must be provided.")
        return values

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
    labels: Optional[Dict[str, str]] = Field(
        None,
        description="Optional labels for ElevenLabs when creating the voice.",
    )
    language: Optional[str] = Field(
        None,
        description="Language of the voice. If not provided, it will be auto-detected from the voice_description.",
    )
    gender: Optional[str] = Field(
        None,
        description="Gender of the voice.",
    )

    class Config:
        schema_extra = {
            "example": {
                "generated_voice_id": "temp_preview_id_from_step1",
                "voice_name": "My New Designed Voice",
                "voice_description": "A custom voice designed from text.",
                "language": "en",
                "gender": "male",
                "labels": {"use_case": "audiobook"},
            },
        }


class AssistantPhotoUploadResponse(BaseModel):
    gcs_url: str = Field(
        ...,
        description="GCS URL of the uploaded photo",
        example="gs://your-bucket-name/user_id/image_uuid.jpg",
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
