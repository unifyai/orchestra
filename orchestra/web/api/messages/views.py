import logging
import os

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.db.dao.api_message_dao import ApiMessageDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.messages.schema import (
    MessageComplete,
    MessageSend,
    MessageStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()

ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


async def _dispatch_to_adapters(
    assistant_id: int,
    api_message_id: str,
    body: str,
) -> None:
    if not ADAPTERS_URL:
        logger.warning("UNITY_ADAPTERS_URL not set, skipping adapter dispatch")
        return
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ADAPTERS_URL}/api/message",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "assistant_id": str(assistant_id),
                "api_message_id": api_message_id,
                "body": body,
            },
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
        created_at=msg.created_at,
        completed_at=msg.completed_at,
    )


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

    api_message_dao = ApiMessageDAO(session)
    api_message = api_message_dao.create(
        assistant_id=body.assistant_id,
        user_id=request.state.user_id,
        message=body.message,
        organization_id=getattr(request.state, "organization_id", None),
    )

    await _dispatch_to_adapters(
        assistant_id=body.assistant_id,
        api_message_id=api_message.id,
        body=body.message,
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
    message_id: str,
    request: Request,
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
    api_message_dao = ApiMessageDAO(session)
    api_message = api_message_dao.complete(
        message_id=message_id,
        response=body.response,
    )
    if not api_message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found.",
        )

    return InfoResponse(info=_to_status(api_message))
