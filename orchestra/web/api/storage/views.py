"""
API endpoints for GCS storage operations.

Provides functionality to generate signed URLs and download content
from Google Cloud Storage objects.
"""

import base64
import datetime
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from orchestra.services.bucket_service import BucketService
from orchestra.web.api.storage.schema import (
    DownloadRequest,
    DownloadResponse,
    SignedUrlRequest,
    SignedUrlResponse,
)
from orchestra.web.api.utils.gcp import parse_gcs_url

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

router = APIRouter()


def _validate_bucket(bucket_name: str, bucket_service: BucketService) -> None:
    if not bucket_service.is_allowed_bucket(bucket_name):
        raise HTTPException(
            status_code=403,
            detail="Access to the requested bucket is not permitted",
        )


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'["\r\n\x00/\\]', "_", filename)


@router.post(
    "/storage/signed-url",
    response_model=SignedUrlResponse,
    responses={
        200: {
            "description": "Signed URL generated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "signed_url": "https://storage.googleapis.com/bucket/object?X-Goog-Algorithm=...",
                        "expires_in_minutes": 60,
                    },
                },
            },
        },
        400: {
            "description": "Invalid GCS URI format",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid GCS URI format. Expected gs://bucket-name/object-path",
                    },
                },
            },
        },
        404: {
            "description": "Object not found in GCS",
            "content": {
                "application/json": {
                    "example": {"detail": "Object not found: gs://bucket/path"},
                },
            },
        },
    },
    summary="Generate a signed URL for a GCS object",
    description="""
Generates a temporary signed URL that provides time-limited access to a
Google Cloud Storage object without requiring authentication.

The signed URL can be used to download the object directly via HTTP GET.
    """,
)
def generate_signed_url(
    request: SignedUrlRequest,
    bucket_service: BucketService = Depends(BucketService),
) -> SignedUrlResponse:
    """Generate a signed URL for accessing a GCS object."""
    # Parse the GCS URI
    bucket_name, object_path = parse_gcs_url(request.gcs_uri)

    if not bucket_name or not object_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GCS URI format. Expected gs://bucket-name/object-path",
        )

    _validate_bucket(bucket_name, bucket_service)

    try:
        bucket = bucket_service.storage_client.bucket(bucket_name)
        blob = bucket.blob(object_path)

        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Object not found: {request.gcs_uri}",
            )

        expiration = datetime.timedelta(minutes=request.expiration_minutes)

        signed_url_kwargs = {
            "version": "v4",
            "expiration": expiration,
            "method": "GET",
        }

        if request.download:
            if request.filename:
                filename = _sanitize_filename(request.filename)
            else:
                filename = (
                    object_path.split("/")[-1] if "/" in object_path else object_path
                )
            signed_url_kwargs["response_disposition"] = (
                f'attachment; filename="{filename}"'
            )

        signed_url = blob.generate_signed_url(**signed_url_kwargs)

        return SignedUrlResponse(
            signed_url=signed_url,
            expires_in_minutes=request.expiration_minutes,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate signed URL for {request.gcs_uri}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to generate signed URL",
        )


@router.post(
    "/storage/download",
    response_model=DownloadResponse,
    responses={
        200: {
            "description": "Object content downloaded successfully",
            "content": {
                "application/json": {
                    "example": {
                        "content_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                        "content_type": "image/png",
                        "size_bytes": 68,
                    },
                },
            },
        },
        400: {
            "description": "Invalid GCS URI format",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid GCS URI format. Expected gs://bucket-name/object-path",
                    },
                },
            },
        },
        404: {
            "description": "Object not found in GCS",
            "content": {
                "application/json": {
                    "example": {"detail": "Object not found: gs://bucket/path"},
                },
            },
        },
    },
    summary="Download a GCS object as base64",
    description="""
Downloads the content of a Google Cloud Storage object and returns it
as a base64-encoded string.

This is useful for retrieving binary content (images, files) through
the API without requiring direct GCS access.
    """,
)
def download_object(
    request: DownloadRequest,
    bucket_service: BucketService = Depends(BucketService),
) -> DownloadResponse:
    """Download a GCS object and return its content as base64."""
    # Parse the GCS URI
    bucket_name, object_path = parse_gcs_url(request.gcs_uri)

    if not bucket_name or not object_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GCS URI format. Expected gs://bucket-name/object-path",
        )

    _validate_bucket(bucket_name, bucket_service)

    try:
        bucket = bucket_service.storage_client.bucket(bucket_name)
        blob = bucket.blob(object_path)

        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Object not found: {request.gcs_uri}",
            )

        blob.reload()
        if isinstance(blob.size, (int, float)) and blob.size > MAX_DOWNLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Object exceeds maximum download size of {MAX_DOWNLOAD_BYTES} bytes",
            )

        content = blob.download_as_bytes()
        content_type: Optional[str] = blob.content_type

        content_base64 = base64.b64encode(content).decode("utf-8")

        return DownloadResponse(
            content_base64=content_base64,
            content_type=content_type,
            size_bytes=len(content),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download object {request.gcs_uri}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to download object",
        )
