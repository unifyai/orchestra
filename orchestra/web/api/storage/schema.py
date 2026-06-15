"""Schema definitions for storage API endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class SignedUrlRequest(BaseModel):
    """Request body for generating a signed URL."""

    gcs_uri: str = Field(
        ...,
        description="GCS URI of the object, for example gs://bucket/path/to/object",
        examples=["gs://bucket/images/photo.jpg"],
    )
    expiration_minutes: int = Field(
        default=60,
        ge=1,
        le=10080,
        description="URL expiration time in minutes.",
    )
    download: bool = Field(
        default=False,
        description="When true, the URL includes an attachment Content-Disposition.",
    )
    filename: Optional[str] = Field(
        default=None,
        description="Filename override for attachment downloads.",
    )


class SignedUrlResponse(BaseModel):
    """Response body containing the signed URL."""

    signed_url: str
    expires_in_minutes: int


class DownloadRequest(BaseModel):
    """Request body for downloading object content."""

    gcs_uri: str = Field(
        ...,
        description="GCS URI of the object, for example gs://bucket/path/to/object",
        examples=["gs://bucket/images/photo.jpg"],
    )


class DownloadResponse(BaseModel):
    """Response body containing object content."""

    content_base64: str
    content_type: Optional[str] = None
    size_bytes: int
