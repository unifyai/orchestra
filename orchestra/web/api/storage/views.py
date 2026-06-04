"""API endpoints for Google Cloud Storage objects."""

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

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

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
    summary="Generate a signed URL for a GCS object",
)
def generate_signed_url(
    request: SignedUrlRequest,
    bucket_service: BucketService = Depends(BucketService),
) -> SignedUrlResponse:
    """Generate a temporary signed URL for a GCS object."""
    bucket_name, object_path = parse_gcs_url(request.gcs_uri)
    if not bucket_name or not object_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GCS URI format. Expected gs://bucket/object-path",
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

        signed_url_kwargs = {
            "version": "v4",
            "expiration": datetime.timedelta(minutes=request.expiration_minutes),
            "method": "GET",
        }

        if request.download:
            filename = request.filename or object_path.split("/")[-1]
            signed_url_kwargs["response_disposition"] = (
                f'attachment; filename="{_sanitize_filename(filename)}"'
            )

        return SignedUrlResponse(
            signed_url=blob.generate_signed_url(**signed_url_kwargs),
            expires_in_minutes=request.expiration_minutes,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to generate signed URL for %s: %s", request.gcs_uri, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to generate signed URL",
        ) from exc


@router.post(
    "/storage/download",
    response_model=DownloadResponse,
    summary="Download a GCS object as base64",
)
def download_object(
    request: DownloadRequest,
    bucket_service: BucketService = Depends(BucketService),
) -> DownloadResponse:
    """Download a GCS object and return its content as base64."""
    bucket_name, object_path = parse_gcs_url(request.gcs_uri)
    if not bucket_name or not object_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GCS URI format. Expected gs://bucket/object-path",
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
                detail=(
                    "Object exceeds maximum download size of "
                    f"{MAX_DOWNLOAD_BYTES} bytes"
                ),
            )

        content = blob.download_as_bytes()
        content_type: Optional[str] = blob.content_type

        return DownloadResponse(
            content_base64=base64.b64encode(content).decode("utf-8"),
            content_type=content_type,
            size_bytes=len(content),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to download object %s: %s", request.gcs_uri, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to download object",
        ) from exc
