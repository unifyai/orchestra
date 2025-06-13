from datetime import datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field, HttpUrl
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
                "email": "ada.lovelace@unify.ai",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "user_phone": "+15551234567",
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
                "email": "ada.lovelace@unify.ai",
                "phone": "+15551234567",
                "user_phone": "+15551234567",
                "whatsapp_sid": "whatsapp:+1234567890",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                "agent_id": "12345",
                "created_at": "2025-04-25T10:30:00Z",
                "updated_at": "2025-04-26T14:15:00Z",
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
    whatsapp_sid: Optional[str] = Field(
        None,
        description="WhatsApp SID for Twilio integration",
        example="whatsapp:+1234567890",
    )
    voice_id: Optional[str] = Field(  # This is Cartesia's voice ID
        None,
        description="Id of the voice (Cartesia ID) to use for the assistant",
        example="bf0a246a-8642-498a-9950-80c35e9276b5",
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
                "email": "ada.lovelace@newdomain.com",
                "whatsapp_sid": "whatsapp:+1234567890",
                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
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
    gender: str = Field(
        ...,
        description="Gender of the voice",
        example="female",
    )
    language: str = Field(
        ...,
        description="Language code of the voice",
        example="en",
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


class VoiceLocalizeRequest(BaseModel):
    base_cartesia_voice_id: str = Field(
        ...,
        description="Cartesia Voice ID of the voice to localize",
    )
    name: str = Field(..., description="Name for the new localized voice")
    target_language: str = Field(
        ...,
        description="Target language for localization (e.g., 'es')",
    )
    original_speaker_gender: str = Field(
        ...,
        description="Gender of the original speaker ('female' or 'male')",
    )
    description: Optional[str] = Field(
        None,
        description="Optional description for the voice",
    )
    dialect: Optional[str] = Field(
        None,
        description="Optional dialect for localization",
    )


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
