"""Schema definitions for storage API endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class SignedUrlRequest(BaseModel):
    """Request body for generating a signed URL."""

    gcs_uri: str = Field(
        ...,
        description="GCS URI of the object (e.g., gs://bucket-name/path/to/object)",
        examples=["gs://my-bucket/images/photo.jpg"],
    )
    expiration_minutes: int = Field(
        default=60,
        ge=1,
        le=10080,  # Max 7 days
        description="URL expiration time in minutes (1-10080, default 60)",
    )
    download: bool = Field(
        default=False,
        description="If true, the signed URL will force download with Content-Disposition: attachment",
    )


class SignedUrlResponse(BaseModel):
    """Response body containing the signed URL."""

    signed_url: str = Field(
        ...,
        description="Temporary signed URL for accessing the object",
    )
    expires_in_minutes: int = Field(
        ...,
        description="URL expiration time in minutes",
    )


class DownloadRequest(BaseModel):
    """Request body for downloading object content."""

    gcs_uri: str = Field(
        ...,
        description="GCS URI of the object (e.g., gs://bucket-name/path/to/object)",
        examples=["gs://my-bucket/images/photo.jpg"],
    )


class DownloadResponse(BaseModel):
    """Response body containing the object content."""

    content_base64: str = Field(
        ...,
        description="Base64-encoded content of the object",
    )
    content_type: Optional[str] = Field(
        default=None,
        description="MIME type of the object if available",
    )
    size_bytes: int = Field(
        ...,
        description="Size of the object in bytes",
    )
