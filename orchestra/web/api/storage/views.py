"""API endpoints for Google Cloud Storage objects."""

import base64
import datetime
import logging
import os
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.services.bucket_service import BucketService, create_bucket_service
from orchestra.services.local_bucket_service import LocalBucketService
from orchestra.settings import settings
from orchestra.web.api.storage.schema import (
    DownloadRequest,
    DownloadResponse,
    SignedUrlRequest,
    SignedUrlResponse,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant
from orchestra.web.api.utils.gcp import parse_gcs_url

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

router = APIRouter()
public_router = APIRouter()


@public_router.get(
    "/storage/local/{bucket_name}/{object_path:path}",
    summary="Serve a locally stored object (self-host only)",
)
def serve_local_object(bucket_name: str, object_path: str):
    """Stream an object from the local bucket directory for self-host deployments."""
    if not settings.is_self_host:
        raise HTTPException(status_code=404, detail="Not found")

    bucket_service = create_bucket_service()
    if not isinstance(bucket_service, LocalBucketService):
        raise HTTPException(status_code=404, detail="Not found")
    if not bucket_service.is_allowed_bucket(bucket_name):
        raise HTTPException(status_code=403, detail="Bucket not permitted")

    try:
        content, content_type = bucket_service.read_local_object(
            bucket_name,
            object_path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Object not found") from exc

    from fastapi.responses import Response

    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
    )


def _validate_bucket(bucket_name: str, bucket_service: BucketService) -> None:
    if not bucket_service.is_allowed_bucket(bucket_name):
        raise HTTPException(
            status_code=403,
            detail="Access to the requested bucket is not permitted",
        )


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'["\r\n\x00/\\]', "_", filename)


# Recording objects are named {deploy_env}/{assistant_id}/{room}_{ts}.mp3 by the
# comms gateway's egress request. The assistant segment is what authorizes
# playback, so a path without one cannot be served.
_RECORDING_PATH_RE = re.compile(r"^[^/]+/(?P<assistant_id>\d+)/[^/]+\.mp3$")


def _signed_recording_url(
    request_fastapi: Request,
    session: Session,
    *,
    gcs_uri: str,
    object_path: str,
) -> SignedUrlResponse:
    """Authorize a call-recording read, then have the comms gateway sign it.

    Access follows the assistant, matching how the Console reads the transcript
    of the same call: whoever may see the assistant may hear its calls. The
    check has to happen here because the gateway is called with the platform
    admin key and so cannot see the end user.
    """
    match = _RECORDING_PATH_RE.match(object_path)
    if not match:
        raise HTTPException(status_code=400, detail="Not a recording object path")
    require_owned_assistant(
        request_fastapi,
        int(match.group("assistant_id")),
        session,
    )

    comms_url = os.environ.get("UNITY_COMMS_URL", "").rstrip("/")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if not comms_url or not admin_key:
        logger.error(
            "Cannot sign call recording: UNITY_COMMS_URL / ORCHESTRA_ADMIN_KEY unset",
        )
        raise HTTPException(
            status_code=503,
            detail="Recording playback is not configured",
        )

    try:
        response = httpx.post(
            f"{comms_url}/phone/recording-url",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"gcs_uri": gcs_uri},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.error("Recording signing request to comms failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Recording service unavailable",
        ) from exc

    if response.status_code != 200:
        # Pass the outcome through rather than collapsing it: 404 tells the
        # caller the recording was never written (a pre-gate failed egress still
        # left a URL on its exchange), which is a different thing to say than
        # "something went wrong".
        detail = "Could not load recording"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = str(body["detail"])
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    payload = response.json()
    return SignedUrlResponse(
        signed_url=payload["signed_url"],
        expires_in_minutes=payload.get("expires_in_minutes", 60),
    )


@router.post(
    "/storage/signed-url",
    response_model=SignedUrlResponse,
    summary="Generate a signed URL for a GCS object",
)
def generate_signed_url(
    request: SignedUrlRequest,
    request_fastapi: Request,
    bucket_service: BucketService = Depends(create_bucket_service),
    session: Session = Depends(get_db_session),
) -> SignedUrlResponse:
    """Generate a temporary signed URL for a GCS object."""
    bucket_name, object_path = parse_gcs_url(request.gcs_uri)
    if not bucket_name or not object_path:
        raise HTTPException(
            status_code=400,
            detail="Invalid GCS URI format. Expected gs://bucket/object-path",
        )

    _validate_bucket(bucket_name, bucket_service)

    # Call recordings live in the comms project, which this service has no
    # access to. Authorize here (only this service knows the caller) and let
    # the comms gateway sign, since it holds the bucket's service-account key.
    # Self-host is exempt: it keeps recordings on local disk and serves them
    # through the local-object route, with no comms gateway in the picture.
    if bucket_name == bucket_service.call_recordings_bucket_name and not isinstance(
        bucket_service,
        LocalBucketService,
    ):
        return _signed_recording_url(
            request_fastapi,
            session,
            gcs_uri=request.gcs_uri,
            object_path=object_path,
        )

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
    bucket_service: BucketService = Depends(create_bucket_service),
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
