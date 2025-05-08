from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.recording_dao import RecordingDAO
from orchestra.services.call_recording_service import CallRecordingService
from orchestra.web.api.assistant.schema import (
    AssistantCreate,
    AssistantRead,
    AssistantUpdate,
    InfoResponse,
    RecordingCreate,
    RecordingInfo,
)

router = APIRouter()


@router.post(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a new assistant",
    description="Creates a new assistant for the authenticated user with the specified configuration.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "agent_id": "123",
                            "first_name": "Alice",
                            "surname": "Smith",
                            "age": 25,
                            "weekly_limit": 40.0,
                            "max_parallel": 3,
                            "created_at": "2025-04-25T12:00:00Z",
                            "updated_at": "2025-04-25T12:00:00Z",
                            "phone": "+1-555-123-4567",
                            "email": "alice.smith@example.com",
                        },
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "first_name"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def create_assistant(
    assistant_in: AssistantCreate,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> InfoResponse[AssistantRead]:
    """
    Create a new assistant for the authenticated user.

    This endpoint allows users to create a personalized assistant with specific
    attributes like name, age, and operational limits. Each assistant is tied
    to the authenticated user's account.
    """
    try:
        assistant = dao.create_assistant(
            user_id=request.state.user_id,
            first_name=assistant_in.first_name,
            surname=assistant_in.surname,
            age=assistant_in.age,
            region=assistant_in.region,
            profile_photo=assistant_in.profile_photo,
            about=assistant_in.about,
            weekly_limit=Decimal(assistant_in.weekly_limit),
            max_parallel=assistant_in.max_parallel,
            phone=assistant_in.phone,
            email=assistant_in.email,
        )

        return InfoResponse(
            info=AssistantRead(
                agent_id=str(assistant.agent_id),
                first_name=assistant.first_name,
                surname=assistant.surname,
                age=assistant.age,
                region=assistant.region,
                profile_photo=assistant.profile_photo,
                about=assistant.about,
                weekly_limit=float(assistant.weekly_limit),
                max_parallel=assistant.max_parallel,
                created_at=assistant.created_at,
                updated_at=assistant.updated_at,
                phone=assistant.phone,
                email=assistant.email,
            ),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error creating assistant: {str(e)}",
        )


@router.get(
    "/assistant",
    response_model=InfoResponse[List[AssistantRead]],
    status_code=status.HTTP_200_OK,
    summary="List all assistants",
    description="Returns a list of all assistants belonging to the authenticated user.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "List of assistants retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": [
                            {
                                "agent_id": "123",
                                "first_name": "Alice",
                                "surname": "Smith",
                                "age": 25,
                                "weekly_limit": 40.0,
                                "max_parallel": 3,
                                "phone": "+1-555-123-4567",
                                "email": "alice.smith@example.com",
                                "region": "North America",
                                "profile_photo": "https://example.com/photos/alice.jpg",
                                "about": "Mathematician and writer known for work on Analytical Engine",
                                "created_at": "2025-04-25T12:00:00Z",
                                "updated_at": "2025-04-25T12:00:00Z",
                            },
                            {
                                "agent_id": "456",
                                "first_name": "Bob",
                                "surname": "Jones",
                                "age": 30,
                                "weekly_limit": 35.5,
                                "max_parallel": 2,
                                "phone": "+1-555-987-6543",
                                "email": "bob.jones@example.com",
                                "region": "South America",
                                "profile_photo": "https://example.com/photos/bob.jpg",
                                "about": "Machine learning expert with focus on computer vision",
                                "created_at": "2025-04-24T10:30:00Z",
                                "updated_at": "2025-04-24T10:30:00Z",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def list_assistants(
    request: Request,
    dao: AssistantDAO = Depends(),
) -> InfoResponse[List[AssistantRead]]:
    """
    List all assistants for the authenticated user.

    Retrieves all assistants created by the current user, including their
    configuration details and operational limits.
    """
    try:
        assistants = dao.list_assistants_for_user(request.state.user_id)
        return InfoResponse(
            info=[
                AssistantRead(
                    agent_id=str(a.agent_id),
                    first_name=a.first_name,
                    surname=a.surname,
                    age=a.age,
                    region=a.region,
                    profile_photo=a.profile_photo,
                    about=a.about,
                    weekly_limit=float(a.weekly_limit),
                    max_parallel=a.max_parallel,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                    phone=a.phone,
                    email=a.email,
                )
                for a in assistants
            ],
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error fetching assistants: {str(e)}",
        )


@router.delete(
    "/assistant/{assistant_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete an assistant",
    description="Deletes a specific assistant by ID for the authenticated user.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Assistant deleted successfully"},
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
    },
)
def delete_assistant(
    assistant_id: int,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> InfoResponse[str]:
    """
    Delete an assistant by ID for the authenticated user.

    Permanently removes the specified assistant from the user's account.
    This action cannot be undone.
    """
    try:
        dao.delete_assistant(user_id=request.state.user_id, agent_id=assistant_id)
        return InfoResponse(info="Assistant deleted successfully")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )


@router.patch(
    "/assistant/{assistant_id}/config",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Update assistant configuration",
    description="Updates the configuration parameters of an existing assistant.",
    tags=["Assistants"],
    responses={
        200: {
            "description": "Assistant configuration updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "agent_id": "123",
                            "first_name": "Alice",
                            "surname": "Smith",
                            "age": 25,
                            "weekly_limit": 45.0,
                            "max_parallel": 4,
                            "about": "Award-winning mathematician specializing in algorithm development",
                            "phone": "+1-555-987-6543",
                            "email": "alice.smith@example.com",
                            "region": "North America",
                            "profile_photo": "https://example.com/photos/alice.jpg",
                            "created_at": "2025-04-25T12:00:00Z",
                            "updated_at": "2025-04-25T14:30:00Z",
                        },
                    },
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "email"],
                                "msg": "value is not a valid email address",
                                "type": "value_error.email",
                            },
                        ],
                    },
                },
            },
        },
    },
)
def update_assistant_config(
    assistant_id: int,
    update: AssistantUpdate,
    request: Request,
    dao: AssistantDAO = Depends(),
) -> InfoResponse[AssistantRead]:
    """
    Update about, phone, email, weekly_limit, and/or max_parallel for an existing assistant.

    Allows partial updates to an assistant's configuration. Only the fields
    provided in the request will be updated, while others remain unchanged.
    """
    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        updated = dao.update_assistant(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            about=update.about,
            phone=update.phone,
            email=update.email,
            weekly_limit=weekly_limit,
            max_parallel=update.max_parallel,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )
        return InfoResponse(
            info=AssistantRead(
                agent_id=str(updated.agent_id),
                first_name=updated.first_name,
                surname=updated.surname,
                age=updated.age,
                region=updated.region,
                profile_photo=updated.profile_photo,
                about=updated.about,
                weekly_limit=float(updated.weekly_limit),
                max_parallel=updated.max_parallel,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
                phone=updated.phone,
                email=updated.email,
            ),
        )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error updating assistant config: {str(e)}",
        )


@router.post(
    "/assistant/{assistant_id}/recordings",
    response_model=InfoResponse[RecordingInfo],
    status_code=status.HTTP_200_OK,
    summary="Add a call recording for an assistant",
    description="Uploads a new call recording for the specified assistant.",
    responses={
        200: {
            "description": "Recording added successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "id": 123,
                            "url": "https://storage.example.com/recordings/call_123.mp3",
                            "created_at": "2025-05-08T14:30:00Z",
                        },
                    },
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
        400: {
            "description": "Recording Error",
            "content": {
                "application/json": {
                    "example": {"detail": "Error processing recording."},
                },
            },
        },
    },
)
async def create_recording(
    assistant_id: int,
    recording: RecordingCreate,
    request: Request,
    recording_service: CallRecordingService = Depends(),
) -> InfoResponse[RecordingInfo]:
    """
    Add a new call recording for the specified assistant.

    This endpoint allows uploading a call recording by providing base64-encoded audio data.
    The system will decode the audio, store it securely, and associate it with the assistant.
    """
    try:
        mime = recording.content_type or "application/octet-stream"
        recording_model = await recording_service.record_call_from_raw(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            recording_raw=recording.recording_raw,
            content_type=mime,
        )

        return InfoResponse(
            info=RecordingInfo(
                id=recording_model.id,
                url=recording_model.url,
                created_at=recording_model.created_at,
            ),
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error processing recording: {str(e)}",
        )


@router.get(
    "/assistant/{assistant_id}/recordings",
    response_model=InfoResponse[List[RecordingInfo]],
    status_code=status.HTTP_200_OK,
    summary="List all recordings for an assistant",
    description="Returns a list of all call recordings for the specified assistant.",
    responses={
        200: {
            "description": "List of recordings retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": [
                            {
                                "id": 123,
                                "url": "https://storage.example.com/recordings/call_123.mp3",
                                "created_at": "2025-05-08T14:30:00Z",
                            },
                            {
                                "id": 124,
                                "url": "https://storage.example.com/recordings/call_124.mp3",
                                "created_at": "2025-05-09T10:15:00Z",
                            },
                        ],
                    },
                },
            },
        },
        404: {
            "description": "Assistant Not Found",
            "content": {
                "application/json": {"example": {"detail": "Assistant not found."}},
            },
        },
    },
)
def list_recordings(
    assistant_id: int,
    request: Request,
    recording_dao: RecordingDAO = Depends(),
    assistant_dao: AssistantDAO = Depends(),
) -> InfoResponse[List[RecordingInfo]]:
    """
    List all call recordings for the specified assistant.

    Retrieves all call recordings associated with the assistant, including their
    URLs and creation timestamps.
    """
    try:
        # Verify assistant exists and belongs to user
        assistant = assistant_dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        recordings = recording_dao.list_recordings(agent_id=assistant_id)

        return InfoResponse(
            info=[
                RecordingInfo(
                    id=recording.id,
                    url=recording.url,
                    created_at=recording.created_at,
                )
                for recording in recordings
            ],
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error fetching recordings: {str(e)}",
        )


@router.delete(
    "/assistant/{assistant_id}/recordings/{recording_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a recording",
    description="Deletes a specific call recording by ID for the specified assistant.",
    responses={
        200: {
            "description": "Recording deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Recording deleted successfully"},
                },
            },
        },
        404: {
            "description": "Recording Not Found",
            "content": {
                "application/json": {"example": {"detail": "Recording not found."}},
            },
        },
    },
)
def delete_recording(
    assistant_id: int,
    recording_id: int,
    request: Request,
    recording_dao: RecordingDAO = Depends(),
    assistant_dao: AssistantDAO = Depends(),
) -> InfoResponse[str]:
    """
    Delete a call recording by ID for the specified assistant.

    Permanently removes the specified recording from the system.
    This action cannot be undone.
    """
    try:
        # Verify assistant exists and belongs to user
        assistant = assistant_dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # Delete the recording
        success = recording_dao.delete_recording(
            recording_id=recording_id,
            agent_id=assistant_id,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recording not found.",
            )

        return InfoResponse(info="Recording deleted successfully")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error deleting recording: {str(e)}",
        )
