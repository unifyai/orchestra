from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.recording_dao import RecordingDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.bucket_service import BucketService
from orchestra.services.call_recording_service import CallRecordingService
from orchestra.settings import settings
from orchestra.web.api.assistant.schema import (
    AssistantCreate,
    AssistantRead,
    AssistantUpdate,
    InfoResponse,
    RecordingCreate,
    RecordingInfo,
    VoiceCreate,
    VoiceRead,
)
from orchestra.web.api.utils.assistant_infra import (
    delete_cloud_run_job,
    delete_email,
    delete_phone_number,
    delete_pubsub_topic,
    stop_cloud_run_job,
)


def normalize_phone_parameter(raw_phone: Optional[str]) -> Optional[str]:
    """
    Normalize phone parameter that may have been URL-decoded.
    FastAPI URL-decodes '+' to space, so convert leading space back to '+'.
    """
    if raw_phone and raw_phone.startswith(" "):
        return "+" + raw_phone[1:]
    return raw_phone


router = APIRouter()
admin_router = APIRouter()

ASSISTANT_CREATION_COST = Decimal("10.0")


@router.post(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a new assistant",
    description="Creates a new assistant for the authenticated user with the specified configuration. This action will deduct credits from the user account.",
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
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                        },
                    },
                },
            },
        },
        402: {
            "description": "Insufficient credits",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Insufficient credits to create an assistant.",
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Create a new assistant for the authenticated user.

    This endpoint allows users to create a personalized assistant with specific
    attributes like name, age, and operational limits. Each assistant is tied
    to the authenticated user's account. Creating an assistant incurs a credit cost.
    """
    user_id = request.state.user_id
    users_dao = UsersDAO(session)
    assistant_dao = AssistantDAO(session)
    assistant = None

    # Phase 1: Pre-checks and prepare assistant data
    try:
        if not settings.is_staging:
            user = users_dao.get_user_with_id(user_id)
            if user.credits < ASSISTANT_CREATION_COST:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to create an assistant.",
                )

        parsed_weekly_limit = (
            Decimal(assistant_in.weekly_limit)
            if assistant_in.weekly_limit is not None
            else None
        )

        assistant = assistant_dao.create_assistant(
            user_id=user_id,
            first_name=assistant_in.first_name,
            surname=assistant_in.surname,
            age=assistant_in.age,
            region=assistant_in.region,
            profile_photo=assistant_in.profile_photo,
            about=assistant_in.about,
            weekly_limit=parsed_weekly_limit,
            max_parallel=assistant_in.max_parallel,
            phone=assistant_in.phone,
            email=assistant_in.email,
            whatsapp_sid=assistant_in.whatsapp_sid,
            voice_id=assistant_in.voice_id,
        )

        # assistant_id = assistant.agent_id
        # # Infrastructure creation with rollback on failure
        # created_email = None
        # created_phone = None
        # created_whatsapp = None
        # created_pubsub = None
        # created_job = None
        # started_job = False

        # try:
        #     # Step 1: create email
        #     if assistant_in.email:
        #         email_local = (
        #             assistant_in.email.split("@")[0]
        #             if "@" in assistant_in.email
        #             else assistant_in.email
        #         )
        #         email_response = create_email(
        #             email_local,
        #             assistant_in.first_name,
        #             assistant_in.surname,
        #         )
        #         if "detail" in email_response:
        #             raise Exception(
        #                 f"Email creation failed: {email_response['detail']}",
        #             )
        #         created_email = email_response.get("email") or assistant_in.email

        #         # Step 2: watch email
        #         watch_response = watch_email(created_email)
        #         if "detail" in watch_response:
        #             raise Exception(
        #                 f"Email watch setup failed: {watch_response['detail']}",
        #             )

        #     # Step 3: create phone number (only if not provided)
        #     if not assistant_in.phone:
        #         phone_response = create_phone_number()
        #         if "detail" in phone_response:
        #             raise Exception(
        #                 f"Phone number creation failed: {phone_response['detail']}",
        #             )
        #         created_phone = phone_response.get("phone_number")
        #     else:
        #         # Use the provided phone number
        #         created_phone = assistant_in.phone

        #     # Step 4: create whatsapp sender
        #     if created_phone:
        #         whatsapp_response = create_whatsapp_sender(
        #             created_phone,
        #             assistant_in.first_name,
        #             assistant_in.surname,
        #         )
        #         if "detail" in whatsapp_response:
        #             raise Exception(
        #                 f"WhatsApp sender creation failed: {whatsapp_response['detail']}",
        #             )
        #         created_whatsapp = whatsapp_response.get("whatsapp_sid")

        #     # Step 5: create pubsub topic
        #     pubsub_response = create_pubsub_topic(str(assistant_id))
        #     if "detail" in pubsub_response:
        #         raise Exception(
        #             f"Pubsub topic creation failed: {pubsub_response['detail']}",
        #         )
        #     created_pubsub = True

        #     # Step 6: create cloud run job
        #     job_response = create_cloud_run_job(
        #         assistant_id=str(assistant_id),
        #         user_name=f"{assistant_in.first_name} {assistant_in.surname}",
        #         assistant_number=created_phone or assistant_in.phone or "",
        #         user_number="",  # This would need to be provided or retrieved from user data
        #     )
        #     if "detail" in job_response:
        #         raise Exception(
        #             f"Cloud Run job creation failed: {job_response['detail']}",
        #         )
        #     created_job = True

        #     # Step 7: start cloud run job
        #     start_response = start_cloud_run_job(str(assistant_id))
        #     if "detail" in start_response:
        #         raise Exception(
        #             f"Cloud Run job start failed: {start_response['detail']}",
        #         )
        #     started_job = True

        #     # Update assistant with created infrastructure details
        #     if created_email or created_phone or created_whatsapp:
        #         assistant = assistant_dao.update_assistant(
        #             user_id=user_id,
        #             agent_id=assistant_id,
        #             email=created_email or assistant.email,
        #             phone=created_phone or assistant.phone,
        #             whatsapp_sid=created_whatsapp or assistant.whatsapp_sid,
        #         )

        # except Exception as infra_error:
        #     # Rollback infrastructure in reverse order
        #     rollback_errors = []

        #     if started_job:
        #         try:
        #             stop_cloud_run_job(str(assistant_id))
        #         except Exception as e:
        #             rollback_errors.append(f"Failed to stop job: {str(e)}")

        #     if created_job:
        #         try:
        #             delete_cloud_run_job(str(assistant_id))
        #         except Exception as e:
        #             rollback_errors.append(f"Failed to delete job: {str(e)}")

        #     if created_pubsub:
        #         try:
        #             delete_pubsub_topic(str(assistant_id))
        #         except Exception as e:
        #             rollback_errors.append(f"Failed to delete pubsub topic: {str(e)}")

        #     if created_phone and not assistant_in.phone:
        #         try:
        #             delete_phone_number(created_phone)
        #         except Exception as e:
        #             rollback_errors.append(f"Failed to delete phone: {str(e)}")

        #     if created_email and "@" not in (assistant_in.email or ""):
        #         try:
        #             delete_email(created_email)
        #         except Exception as e:
        #             rollback_errors.append(f"Failed to delete email: {str(e)}")

        #     # Delete the assistant record since infrastructure failed
        #     try:
        #         assistant_dao.delete_assistant(user_id=user_id, agent_id=assistant_id)
        #     except Exception as e:
        #         rollback_errors.append(f"Failed to delete assistant: {str(e)}")

        #     error_msg = f"Infrastructure setup failed: {str(infra_error)}"
        #     if rollback_errors:
        #         error_msg += f" Rollback issues: {'; '.join(rollback_errors)}"

        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail=error_msg,
        #     )

    except HTTPException:
        raise
    except Exception as e_prepare:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create assistant: {str(e_prepare)}",
        )

    # Phase 2: Deduct credits. The commit within recharge_credit will persist
    # both the assistant and the credit change atomically.
    if not settings.is_staging:
        try:
            users_dao.recharge_credit(
                user_id=user_id,
                quantity=-float(ASSISTANT_CREATION_COST),
            )
        except Exception as e_commit:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Payment processing failed. Assistant creation has been rolled back. Details: {str(e_commit)}",
            )

    if assistant is None:
        # Should ideally not be reached if Phase 1 fails
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create assistant.",
        )

    # Phase 3: Prepare and return response
    return InfoResponse(
        info=AssistantRead(
            agent_id=str(assistant.agent_id),
            first_name=assistant.first_name,
            surname=assistant.surname,
            age=assistant.age,
            region=assistant.region,
            profile_photo=assistant.profile_photo,
            about=assistant.about,
            weekly_limit=(
                float(assistant.weekly_limit)
                if assistant.weekly_limit is not None
                else None
            ),
            max_parallel=assistant.max_parallel,
            created_at=assistant.created_at,
            updated_at=assistant.updated_at,
            phone=assistant.phone,
            email=assistant.email,
            whatsapp_sid=assistant.whatsapp_sid,
            voice_id=assistant.voice_id,
        ),
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
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
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
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
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
    session: Session = Depends(get_db_session),
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this E.164-style value (leading '+' is URL-encoded).",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
) -> InfoResponse[List[AssistantRead]]:
    """
    List all assistants for the authenticated user.

    Retrieves all assistants created by the current user, including their
    configuration details and operational limits.
    """
    # Correct for URL-decoded '+' in query parameters.
    phone = normalize_phone_parameter(phone)

    dao = AssistantDAO(session)
    try:
        assistants = dao.list_assistants_for_user(
            request.state.user_id,
            phone=phone,
            email=email,
        )
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
                    whatsapp_sid=a.whatsapp_sid,
                    voice_id=a.voice_id,
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    """
    Delete an assistant by ID for the authenticated user.

    Permanently removes the specified assistant from the user's account.
    This action cannot be undone.
    """
    dao = AssistantDAO(session)
    try:
        # First get the assistant to retrieve infrastructure details
        assistant = dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # Clean up infrastructure in reverse order
        cleanup_errors = []

        # Stop and delete cloud run job
        try:
            stop_cloud_run_job(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to stop job: {str(e)}")

        try:
            delete_cloud_run_job(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to delete job: {str(e)}")

        # Delete pubsub topic
        try:
            delete_pubsub_topic(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to delete pubsub topic: {str(e)}")

        # Delete phone number if exists
        if assistant.phone:
            try:
                delete_phone_number(assistant.phone)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete phone: {str(e)}")

        # Delete email if exists
        if assistant.email:
            try:
                delete_email(assistant.email)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete email: {str(e)}")

        # Finally delete the assistant record
        dao.delete_assistant(user_id=request.state.user_id, agent_id=assistant_id)

        response_msg = "Assistant deleted successfully"
        if cleanup_errors:
            response_msg += f" (with some cleanup issues: {'; '.join(cleanup_errors)})"

        return InfoResponse(info=response_msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting assistant: {str(e)}",
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
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update about, phone, email, weekly_limit, and/or max_parallel for an existing assistant.

    Allows partial updates to an assistant's configuration. Only the fields
    provided in the request will be updated, while others remain unchanged.
    """
    dao = AssistantDAO(session)
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
            whatsapp_sid=update.whatsapp_sid,
            weekly_limit=weekly_limit,
            max_parallel=update.max_parallel,
            voice_id=update.voice_id,
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
                whatsapp_sid=updated.whatsapp_sid,
                voice_id=updated.voice_id,
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[RecordingInfo]:
    """
    Add a new call recording for the specified assistant.

    This endpoint allows uploading a call recording by providing base64-encoded audio data.
    The system will decode the audio, store it securely, and associate it with the assistant.
    """
    assistant_dao = AssistantDAO(session)
    recording_dao = RecordingDAO(session)
    bucket_service = BucketService()
    recording_service = CallRecordingService(
        assistant_dao=assistant_dao,
        recording_dao=recording_dao,
        bucket_service=bucket_service,
    )
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[RecordingInfo]]:
    """
    List all call recordings for the specified assistant.

    Retrieves all call recordings associated with the assistant, including their
    URLs and creation timestamps.
    """
    assistant_dao = AssistantDAO(session)
    recording_dao = RecordingDAO(session)
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    """
    Delete a call recording by ID for the specified assistant.

    Permanently removes the specified recording from the system.
    This action cannot be undone.
    """
    assistant_dao = AssistantDAO(session)
    recording_dao = RecordingDAO(session)
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


@router.post(
    "/assistant/voice",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a new voice record",
    description="Create a voice that can be used my any assistant during TTS.",
    responses={
        200: {
            "description": "Voice created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": {
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "name": "English Woman Calm 1",
                            "description": "Calm and relaxting voice of an english-speaking woman",
                            "gender": "female",
                            "language": "en",
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
                                "loc": ["body", "name"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            },
                        ],
                    },
                },
            },
        },
    },
    tags=["Voices"],
)
async def create_voice(
    voice_in: VoiceCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[VoiceRead]:
    """
    Create a new voice record in the database after it has been created/localized via Cartesia.
    """
    dao = VoiceDAO(session)
    try:
        voice = dao.create_voice(
            user_id=request.state.user_id,
            voice_id=voice_in.voice_id,  # This is Cartesia's ID
            name=voice_in.name,
            description=voice_in.description,
            gender=voice_in.gender,
            language=voice_in.language,
        )

        return InfoResponse(
            info=VoiceRead(
                voice_id=voice.voice_id,
                name=voice.name,
                description=voice.description,
                gender=voice.gender,
                language=voice.language,
            ),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error creating voice: {str(e)}",
        )


@router.get(
    "/assistant/voice",
    response_model=InfoResponse[List[VoiceRead]],
    status_code=status.HTTP_200_OK,
    summary="List all assistant voices for the user.",
    description="Returns a list of all assistant voices created available for the user.",
    responses={
        200: {
            "description": "List of voices retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": [
                            {
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "name": "English Woman Calm 1",
                                "description": "Calm and relaxting voice of an english-speaking woman",
                                "gender": "female",
                                "language": "en",
                            },
                            {
                                "voice_id": "c99d36f3-5ffd-4253-803a-535c1bc9c306",
                                "name": "English Male Deep 1",
                                "description": "A deep, smoooth British man's voice perfect for narration.",
                                "gender": "male",
                                "language": "en",
                            },
                        ],
                    },
                },
            },
        },
        404: {
            "description": "Voice Not Found",
            "content": {
                "application/json": {"example": {"detail": "Voice not found."}},
            },
        },
    },
    tags=["Voices"],
)
def list_voices(
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[VoiceRead]]:
    """
    List all voices saved by the authenticated user.
    """
    dao = VoiceDAO(session)
    try:
        voices = dao.list_voices_for_user(
            user_id=request.state.user_id,
        )

        return InfoResponse(
            info=[
                VoiceRead(
                    voice_id=voice.voice_id,
                    name=voice.name,
                    description=voice.description,
                    language=voice.language,
                    gender=voice.gender,
                )
                for voice in voices
            ],
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error fetching user voices: {str(e)}",
        )


@router.delete(
    "/assistant/voice/{voice_id}",
    status_code=status.HTTP_200_OK,
    response_model=InfoResponse[str],
    summary="Delete a user's voice record",
    description="Deletes a specific voice record by its Cartesia ID for the authenticated user. This does NOT delete the voice from Cartesia itself, that should be a separate Cartesia API call if needed.",
    responses={
        200: {
            "description": "Voice deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Voice deleted successfully"},
                },
            },
        },
        404: {
            "description": "Voice not found",
            "content": {
                "application/json": {"example": {"detail": "Voice not found."}},
            },
        },
    },
    tags=["Voices"],
)
def delete_voice(
    voice_id: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    dao = VoiceDAO(session)
    try:
        dao.delete_voice(user_id=request.state.user_id, voice_id=voice_id)
        return InfoResponse(info="Voice deleted successfully.")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting voice record: {str(e)}",
        )


##################
# Admin endpoints #
##################


@admin_router.get(
    "/assistant/emails",
    response_model=InfoResponse[List[str]],
    summary="Admin: list all assistant email addresses",
)
async def admin_list_assistant_emails(
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[str]]:
    dao = AssistantDAO(session)
    """Return every non-null email address that has been set on an Assistant."""
    emails = dao.list_all_assistant_emails()
    return InfoResponse(info=emails)
