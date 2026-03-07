import os
import time

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
from orchestra.web.api.utils.gcp import send_pubsub_msg

router = APIRouter()
admin_router = APIRouter()

ASSISTANT_PUBSUB_PROJECT_ID = os.environ.get(
    "ASSISTANT_PUBSUB_PROJECT_ID",
    "responsive-city-458413-a2",
)
IS_STAGING = os.environ.get("ORCHESTRA_ENVIRONMENT") in ("staging", "pytest")


def _pubsub_topic(assistant_id: int) -> str:
    suffix = "-staging" if IS_STAGING else ""
    return (
        f"projects/{ASSISTANT_PUBSUB_PROJECT_ID}"
        f"/topics/unity-{assistant_id}{suffix}"
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

    send_pubsub_msg(
        _pubsub_topic(body.assistant_id),
        {
            "thread": "api_message",
            "publish_timestamp": time.time(),
            "event": {
                "api_message_id": api_message.id,
                "content": body.message,
                "contact_id": 1,
            },
        },
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
