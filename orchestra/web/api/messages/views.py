import datetime
import logging
import os

import httpx
from fastapi import Depends, File, Form, HTTPException, Path, UploadFile, status
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.db.dao.api_message_dao import ApiMessageDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.bucket_service import BucketService
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.messages.schema import (
    MessageComplete,
    MessageSend,
    MessageStatus,
)
from orchestra.web.api.utils.gcp import parse_gcs_url

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()

ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADAPTERS_URL_PREVIEW = os.environ.get("UNITY_ADAPTERS_URL_PREVIEW")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")

MAX_ATTACHMENTS_PER_MESSAGE = 10


def _adapters_url_for_deploy_env(deploy_env: str | None) -> str | None:
    if deploy_env == "preview":
        return ADAPTERS_URL_PREVIEW
    return ADAPTERS_URL


def _generate_signed_url(gs_url: str) -> str | None:
    """Best-effort signed URL generation for a gs:// URI. Returns None on failure."""
    try:
        bucket_name, object_path = parse_gcs_url(gs_url)
        if not bucket_name or not object_path:
            return None
        svc = BucketService()
        bucket = svc.storage_client.bucket(bucket_name)
        blob = bucket.blob(object_path)
        if not blob.exists():
            return None
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(hours=1),
            method="GET",
        )
    except Exception:
        logger.debug("Failed to generate signed URL for %s", gs_url, exc_info=True)
        return None


def _enrich_attachments(raw: list | None) -> list[dict]:
    """Add fresh signed URLs to a list of stored attachment dicts."""
    if not raw:
        return []
    enriched = []
    for att in raw:
        att = dict(att)
        gs_url = att.get("gs_url")
        if gs_url:
            signed = _generate_signed_url(gs_url)
            if signed:
                att["signed_url"] = signed
        enriched.append(att)
    return enriched


async def _dispatch_to_adapters(
    assistant_id: int,
    api_message_id: str,
    body: str,
    deploy_env: str | None = None,
    attachments: list[dict] | None = None,
    tags: list[str] | None = None,
) -> None:
    adapters_url = _adapters_url_for_deploy_env(deploy_env)
    if not adapters_url:
        logger.warning("UNITY_ADAPTERS_URL not set, skipping adapter dispatch")
        return
    payload: dict = {
        "assistant_id": str(assistant_id),
        "api_message_id": api_message_id,
        "body": body,
    }
    if attachments:
        payload["attachments"] = attachments
    if tags:
        payload["tags"] = tags
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{adapters_url}/api/message",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            logger.error(
                f"Adapter dispatch failed: {response.status_code} {response.text}",
            )


def _to_status(msg) -> MessageStatus:
    return MessageStatus(
        message_id=msg.id,
        assistant_id=msg.assistant_id,
        message=msg.message,
        status=msg.status,
        response=msg.response,
        tags=msg.tags or [],
        attachments=_enrich_attachments(msg.attachments),
        response_tags=msg.response_tags,
        response_attachments=_enrich_attachments(msg.response_attachments) or None,
        created_at=msg.created_at,
        completed_at=msg.completed_at,
    )


# ---------------------------------------------------------------------------
# Public endpoints (authenticated via API key)
# ---------------------------------------------------------------------------


@router.post(
    "/messages/attachments",
    status_code=status.HTTP_200_OK,
    tags=["Messages"],
    summary="Upload a file attachment for use in a message",
    description=(
        "Upload a file to be attached to a subsequent POST /messages call. "
        "Returns attachment metadata including the gs_url to reference in the "
        "message attachments array."
    ),
)
async def upload_attachment(
    request: Request,
    file: UploadFile = File(..., description="The file to upload as an attachment."),
    assistant_id: int = Form(
        ...,
        description="The ID of the assistant this attachment is for.",
        example=42,
    ),
    session: Session = Depends(get_db_session),
):
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=request.state.user_id,
        agent_id=assistant_id,
        organization_id=getattr(request.state, "organization_id", None),
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    adapters_url = _adapters_url_for_deploy_env(assistant.deploy_env)
    if not adapters_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Attachment upload service unavailable.",
        )

    file_content = await file.read()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{adapters_url}/unify/attachment",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            files={
                "file": (
                    file.filename,
                    file_content,
                    file.content_type or "application/octet-stream",
                ),
            },
            data={"assistant_id": str(assistant_id)},
            timeout=60,
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Attachment upload failed: {resp.text}",
        )
    return resp.json()


@router.post(
    "/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=InfoResponse[MessageStatus],
    tags=["Messages"],
    summary="Send a message to an assistant",
    description=(
        "Send a programmatic message to an assistant. Returns a message_id "
        "that can be polled via GET /messages/{message_id} for the response."
    ),
)
async def send_message(
    body: MessageSend,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[MessageStatus]:
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_id(
        user_id=request.state.user_id,
        agent_id=body.assistant_id,
        organization_id=getattr(request.state, "organization_id", None),
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    if len(body.attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_ATTACHMENTS_PER_MESSAGE} attachments per message.",
        )

    attachments_dicts = [att.model_dump(exclude_none=True) for att in body.attachments]

    api_message_dao = ApiMessageDAO(session)
    api_message = api_message_dao.create(
        assistant_id=body.assistant_id,
        user_id=request.state.user_id,
        message=body.message,
        organization_id=getattr(request.state, "organization_id", None),
        tags=body.tags,
        attachments=attachments_dicts,
    )

    await _dispatch_to_adapters(
        assistant_id=body.assistant_id,
        api_message_id=api_message.id,
        body=body.message,
        deploy_env=assistant.deploy_env,
        attachments=attachments_dicts,
        tags=body.tags,
    )

    return InfoResponse(info=_to_status(api_message))


@router.get(
    "/messages/{message_id}",
    status_code=status.HTTP_200_OK,
    response_model=InfoResponse[MessageStatus],
    tags=["Messages"],
    summary="Poll for a message response",
    description=(
        "Check the status of a previously sent message. "
        "Returns 'processing' while the assistant is working, "
        "and 'completed' (with an optional response) once done."
    ),
)
async def get_message_status(
    message_id: str = Path(
        ...,
        description="The unique message ID returned by POST /messages.",
        example="msg_abc123",
    ),
    request: Request = None,
    session: Session = Depends(get_db_session),
) -> InfoResponse[MessageStatus]:
    api_message_dao = ApiMessageDAO(session)
    api_message = api_message_dao.get_by_id(
        message_id=message_id,
        user_id=request.state.user_id,
        organization_id=getattr(request.state, "organization_id", None),
    )
    if not api_message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found.",
        )

    return InfoResponse(info=_to_status(api_message))


# ---------------------------------------------------------------------------
# Admin endpoints (Unity internal)
# ---------------------------------------------------------------------------


@admin_router.put(
    "/messages/{message_id}/complete",
    status_code=status.HTTP_200_OK,
    response_model=InfoResponse[MessageStatus],
    tags=["Messages"],
    summary="Mark a message as completed",
    description=(
        "Admin endpoint for Unity to mark an API message as completed, "
        "optionally with a response."
    ),
)
async def complete_message(
    message_id: str,
    body: MessageComplete,
    session: Session = Depends(get_db_session),
) -> InfoResponse[MessageStatus]:
    attachments_dicts = (
        [att.model_dump(exclude_none=True) for att in body.attachments]
        if body.attachments
        else None
    )

    api_message_dao = ApiMessageDAO(session)
    api_message = api_message_dao.complete(
        message_id=message_id,
        response=body.response,
        response_tags=body.tags or None,
        response_attachments=attachments_dicts,
    )
    if not api_message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found.",
        )

    return InfoResponse(info=_to_status(api_message))
