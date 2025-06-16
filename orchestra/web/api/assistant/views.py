import logging
import time
from decimal import Decimal
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.recording_dao import RecordingDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.bucket_service import BucketService
from orchestra.services.call_recording_service import CallRecordingService
from orchestra.services.cartesia_service import CartesiaAPIError, CartesiaService
from orchestra.services.replicate_service import ReplicateAPIError, ReplicateService
from orchestra.settings import settings
from orchestra.web.api.assistant.schema import (
    AssistantCreate,
    AssistantPhotoUploadResponse,
    AssistantRead,
    AssistantUpdate,
    InfoResponse,
    PhotoGenerateRequest,
    RecordingCreate,
    RecordingInfo,
    VoiceCreate,
    VoiceLocalizeRequest,
    VoiceRead,
)
from orchestra.web.api.utils.assistant_infra import (
    create_cloud_run_job,
    create_email,
    create_phone_number,
    create_pubsub_topic,
    create_whatsapp_sender,
    delete_cloud_run_job,
    delete_email,
    delete_phone_number,
    delete_pubsub_topic,
    stop_cloud_run_job,
    watch_email,
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
            if user.credits < settings.assistant_creation_cost:
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
            voice_id=assistant_in.voice_id,
            phone=assistant_in.phone,
            email=assistant_in.email,
        )

        # Commit the assistant creation before infrastructure setup
        # This ensures the assistant persists even if we refresh the session later
        session.commit()

        assistant_id = assistant.agent_id
        # Infrastructure creation with rollback on failure
        created_email = None
        created_phone = None
        created_whatsapp = None
        created_pubsub = None
        created_job = None
        started_job = False

        if assistant_in.create_infra:
            try:
                # Step 1: create email
                email_local = (
                    assistant_in.email.split("@")[0]
                    if "@" in assistant_in.email
                    else assistant_in.email
                )
                email_response = create_email(
                    email_local,
                    assistant_in.first_name,
                    assistant_in.surname,
                )
                if "detail" in email_response:
                    raise Exception(
                        f"Email creation failed: {email_response['detail']}",
                    )
                created_email = email_response.get("user").get("primaryEmail")
                print(f"EMAIL CREATED: {created_email}")

                # Step 2: watch email
                time.sleep(10)
                watch_response = watch_email(created_email)
                print(watch_response)
                if "detail" in watch_response:
                    raise Exception(
                        f"Email watch setup failed: {watch_response['detail']}",
                    )
                print(f"EMAIL WATCHED: {created_email}")

                # Step 3: create phone number
                phone_response = create_phone_number()
                if "detail" in phone_response:
                    raise Exception(
                        f"Phone number creation failed: {phone_response['detail']}",
                    )
                created_phone = phone_response.get("phoneNumber")
                print(f"PHONE CREATED: {created_phone}")

                # Step 4: create whatsapp sender
                whatsapp_response = create_whatsapp_sender(
                    created_phone,
                    assistant_in.first_name,
                    assistant_in.surname,
                )
                if "detail" in whatsapp_response:
                    raise Exception(
                        f"WhatsApp sender creation failed: {whatsapp_response['detail']}",
                    )
                created_whatsapp = whatsapp_response.get("sid")
                print(f"WHATSAPP CREATED: {created_whatsapp}")

                # Step 5: create pubsub topic
                pubsub_response = create_pubsub_topic(str(assistant_id))
                if "detail" in pubsub_response:
                    raise Exception(
                        f"Pubsub topic creation failed: {pubsub_response['detail']}",
                    )
                created_pubsub = True
                print(f"PUBSUB CREATED: {assistant_id}")

                # Step 6: create cloud run job
                job_response = create_cloud_run_job(
                    assistant_id=str(assistant_id),
                    user_name=f"{assistant_in.first_name} {assistant_in.surname}",
                    assistant_number=created_phone,
                    user_number=assistant_in.user_phone,
                )
                if "detail" in job_response:
                    raise Exception(
                        f"Cloud Run job creation failed: {job_response['detail']}",
                    )
                created_job = True
                print(f"JOB CREATED: {assistant_id}")

                # Step 7: start cloud run job
                # start_response = start_cloud_run_job(str(assistant_id))
                # if "detail" in start_response:
                #     raise Exception(
                #         f"Cloud Run job start failed: {start_response['detail']}",
                #     )
                # started_job = True
                # print(f"JOB STARTED: {assistant_id}")

                # Refresh database session after long infrastructure operations
                logging.info(
                    f"Refreshing database session after infrastructure setup for assistant {assistant_id}",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)

                # Update assistant with created infrastructure details
                assistant_dao.update_assistant(
                    user_id=user_id,
                    agent_id=assistant_id,
                    email=created_email,
                    phone=created_phone,
                    user_phone=assistant_in.user_phone,
                    whatsapp_sid=created_whatsapp,
                )
                # Commit the infrastructure updates
                session.commit()
                print(f"ASSISTANT UPDATED: {assistant_id}")

                # Retrieve the updated assistant for the final response
                assistant = assistant_dao.get_assistant_by_id(
                    user_id=user_id,
                    agent_id=assistant_id,
                )

            except Exception as infra_error:
                # can't rollback infra if the setup isn't complete so need to wait
                time.sleep(10)

                # Refresh database session to avoid stale connections during rollback
                logging.warning(
                    f"Infrastructure setup failed for assistant {assistant_id}, refreshing session for rollback",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)

                # Rollback infrastructure in reverse order
                rollback_errors = []

                # Rollback infrastructure in reverse order (these could be async)
                if started_job:
                    try:
                        stop_cloud_run_job(str(assistant_id))
                    except Exception as e:
                        rollback_errors.append(f"Failed to stop job: {str(e)}")
                print(f"JOB STOPPED: {assistant_id}")

                if created_job:
                    try:
                        delete_cloud_run_job(str(assistant_id))
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete job: {str(e)}")
                print(f"JOB DELETED: {assistant_id}")

                if created_pubsub:
                    try:
                        delete_pubsub_topic(str(assistant_id))
                    except Exception as e:
                        rollback_errors.append(
                            f"Failed to delete pubsub topic: {str(e)}",
                        )
                print(f"PUBSUB DELETED: {assistant_id}")

                if created_phone:
                    try:
                        delete_phone_number(created_phone)
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete phone: {str(e)}")
                print(f"PHONE DELETED: {created_phone}")

                if created_email:
                    try:
                        delete_email(created_email)
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete email: {str(e)}")
                print(f"EMAIL DELETED: {created_email}")

                # Delete the assistant record since infrastructure failed
                try:
                    assistant_dao.delete_assistant(
                        user_id=user_id,
                        agent_id=assistant_id,
                    )
                    # Commit the assistant deletion
                    session.commit()
                except Exception as e:
                    rollback_errors.append(f"Failed to delete assistant: {str(e)}")
                print(f"ASSISTANT DELETED: {assistant_id}")

                error_msg = f"Infrastructure setup failed: {str(infra_error)}"
                if rollback_errors:
                    error_msg += f" Rollback issues: {'; '.join(rollback_errors)}"
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=error_msg,
                )

    except HTTPException:
        raise
    except Exception as e_prepare:
        print(f"FAILED TO CREATE ASSISTANT: {str(e_prepare)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create assistant: {str(e_prepare)}",
        )

    # Phase 2: Deduct credits. The commit within recharge_credit will persist
    # both the assistant and the credit change atomically.
    if not settings.is_staging:
        try:
            # Refresh session before credit operation to ensure connection is valid
            users_dao.recharge_credit(
                user_id=user_id,
                quantity=-float(settings.assistant_creation_cost),
            )
            session.commit()
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
                    weekly_limit=(
                        float(a.weekly_limit) if a.weekly_limit is not None else None
                    ),
                    max_parallel=a.max_parallel,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                    phone=a.phone,
                    email=a.email,
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
    This action cannot be undone. Associated GCS profile photos will also be deleted.
    """
    bucket_service = BucketService()
    dao = AssistantDAO(session)
    cleanup_errors = []
    try:
        # First get the assistant to retrieve infrastructure details including GCS photo URL
        assistant = dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
        )
        if not assistant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # Delete GCS profile photo if it exists and is a GCS URL from the assistant images bucket
        if assistant.profile_photo and assistant.profile_photo.startswith("gs://"):
            try:
                deleted_from_gcs = bucket_service.delete_assistant_photo(
                    assistant.profile_photo,
                )
                if not deleted_from_gcs:
                    logging.error(
                        f"Profile photo {assistant.profile_photo} for assistant {assistant_id} was not deleted from GCS (either not found, wrong bucket, or other non-critical issue).",
                    )
                    cleanup_errors.append(
                        f"Failed to delete profile photo: {str(e_gcs)}",
                    )
            except Exception as e_gcs:
                logging.error(
                    f"Failed to delete profile photo {assistant.profile_photo} for assistant {assistant_id}: {str(e_gcs)}",
                )
                cleanup_errors.append(f"Failed to delete profile photo: {str(e_gcs)}")

        # Wait before starting other infra cleanup (same as rollback operations)
        time.sleep(10)

        try:
            stop_cloud_run_job(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to stop job: {str(e)}")
        print(f"JOB STOPPED: {assistant_id}")

        # Delete cloud run job
        try:
            delete_cloud_run_job(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to delete job: {str(e)}")
        print(f"JOB DELETED: {assistant_id}")

        # Delete pubsub topic
        try:
            delete_pubsub_topic(str(assistant_id))
        except Exception as e:
            cleanup_errors.append(f"Failed to delete pubsub topic: {str(e)}")
        print(f"PUBSUB DELETED: {assistant_id}")

        # Delete phone number if exists
        if assistant.phone:
            try:
                delete_phone_number(assistant.phone)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete phone: {str(e)}")
        print(f"PHONE DELETED: {assistant.phone}")

        # Delete email if exists (with debug print like rollback)
        if assistant.email:
            try:
                delete_email(assistant.email)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete email: {str(e)}")
        print(f"EMAIL DELETED: {assistant.email}")

        # Finally delete the assistant record (matching rollback error handling)
        try:
            dao.delete_assistant(user_id=request.state.user_id, agent_id=assistant_id)
        except Exception as e:
            cleanup_errors.append(f"Failed to delete assistant: {str(e)}")
        print(f"ASSISTANT DELETED: {assistant_id}")

        response_msg = "Assistant deleted successfully"
        if cleanup_errors:
            response_msg += f" (with some cleanup issues: {'; '.join(cleanup_errors)})"

        return InfoResponse(info=response_msg)
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        final_error_detail = f"Error deleting assistant: {str(e)}"
        if cleanup_errors:
            final_error_detail += (
                f" | Cleanup issues prior to full rollback: {'; '.join(cleanup_errors)}"
            )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=final_error_detail,
        )


@router.patch(
    "/assistant/{assistant_id}/config",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Update assistant configuration",
    description="Updates the configuration parameters of an existing assistant. Profile photo cannot be updated via this endpoint.",
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
    summary="Register an existing Cartesia voice",
    description="Registers an existing Cartesia voice (e.g., a preset) in the Orchestra DB. The voice must already exist in Cartesia.",
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
def register_cartesia_voice(
    voice_in: VoiceCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[VoiceRead]:
    dao = VoiceDAO(session)
    try:

        voice = dao.create_voice(
            user_id=request.state.user_id,
            voice_id=voice_in.voice_id,
            name=voice_in.name,
            description=voice_in.description,
            gender=voice_in.gender,
            language=voice_in.language,
        )
        voice.is_preset = (
            voice_in.is_preset if voice_in.is_preset is not None else False
        )
        session.commit()
        return InfoResponse(
            info=VoiceRead(
                voice_id=voice.voice_id,
                name=voice.name,
                description=voice.description,
                gender=voice.gender,
                language=voice.language,
                is_preset=voice.is_preset,
            ),
        )
    except IntegrityError as e:
        session.rollback()
        if (
            "violates unique constraint" in str(e).lower()
            and "voices_pkey" in str(e).lower()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Voice with ID '{voice_in.voice_id}' already exists in the database.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database error registering voice: {str(e)}",
        )
    except HTTPException as e:
        session.rollback()
        raise e
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error registering voice: {str(e)}",
        )


@router.post(
    "/assistant/voice/clone",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Clone a new voice",
    description="Clones a new voice using an audio file, registers it with Cartesia, and saves it to the database.",
    tags=["Voices"],
)
async def clone_voice_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    name: str = Form(..., example="My Voice Clone"),
    language: str = Form(..., example="en"),
    description: Optional[str] = Form(None, example="A cloned voice for my assistant"),
    file: UploadFile = File(..., example="voice_sample.wav"),
):
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_cartesia_voice_id: Optional[str] = None

    try:
        file_content = await file.read()
        cartesia_response = cartesia_service.clone_voice(
            file_content=file_content,
            file_name=file.filename
            or "audio_clip_default_name",  # Ensure filename is provided
            name=name,
            language=language,
            description=description,
        )

        new_cartesia_voice_id = cartesia_response.get("id")
        if not new_cartesia_voice_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cartesia did not return a voice ID after cloning.",
            )

        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_cartesia_voice_id,
            name=cartesia_response.get("name", name),
            description=cartesia_response.get(
                "description",
                description or f"Cloned voice: {name}",
            ),
            gender=cartesia_response.get("gender", "female"),
            language=cartesia_response.get("language", language),
        )
        db_voice.is_preset = False
        session.commit()

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                is_preset=False,
            ),
        )

    except CartesiaAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Cartesia API error: {e.detail}",
        )
    except IntegrityError as e_db_integrity:  # DB unique constraint violation
        session.rollback()
        if new_cartesia_voice_id:  # If Cartesia voice was created but DB save failed
            logging.warning(
                f"DB save failed for cloned voice {new_cartesia_voice_id} due to integrity error. Attempting Cartesia cleanup.",
            )
            try:
                cartesia_service.delete_voice(new_cartesia_voice_id)
            except Exception as e_cartesia_cleanup:
                logging.error(
                    f"Failed to cleanup Cartesia voice {new_cartesia_voice_id} after DB integrity error: {e_cartesia_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Failed to save cloned voice to database, voice ID might already exist: {str(e_db_integrity)}",
        )
    except Exception as e_generic:
        session.rollback()
        if (
            new_cartesia_voice_id
        ):  # If Cartesia voice was created but another DB error occurred
            logging.warning(
                f"DB operation failed for cloned voice {new_cartesia_voice_id}. Attempting Cartesia cleanup. Error: {str(e_generic)}",
            )
            try:
                cartesia_service.delete_voice(new_cartesia_voice_id)
            except Exception as e_cartesia_cleanup:
                logging.error(
                    f"Failed to cleanup Cartesia voice {new_cartesia_voice_id} after generic DB error: {e_cartesia_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clone and save voice: {str(e_generic)}",
        )


@router.post(
    "/assistant/voice/localize",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Localize an existing voice",
    description="Localizes an existing Cartesia voice to a new language, registers the new version with Cartesia, and saves it to the database.",
    tags=["Voices"],
)
def localize_voice_endpoint(
    localize_request_data: VoiceLocalizeRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
):
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_cartesia_voice_id: Optional[str] = None

    try:
        cartesia_response = cartesia_service.localize_voice(
            base_voice_id=localize_request_data.base_cartesia_voice_id,
            name=localize_request_data.name,
            target_language=localize_request_data.target_language,
            original_speaker_gender=localize_request_data.original_speaker_gender,
            description=localize_request_data.description,
            dialect=localize_request_data.dialect,
        )

        new_cartesia_voice_id = cartesia_response.get("id")
        if not new_cartesia_voice_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cartesia did not return a voice ID after localization.",
            )

        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_cartesia_voice_id,
            name=cartesia_response.get("name", localize_request_data.name),
            description=cartesia_response.get(
                "description",
                localize_request_data.description
                or f"Localized voice: {localize_request_data.name}",
            ),
            gender=cartesia_response.get(
                "gender",
                localize_request_data.original_speaker_gender,
            ),
            language=cartesia_response.get(
                "language",
                localize_request_data.target_language,
            ),
        )
        db_voice.is_preset = False
        session.commit()

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                is_preset=False,
            ),
        )

    except CartesiaAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Cartesia API error: {e.detail}",
        )
    except IntegrityError as e_db_integrity:
        session.rollback()
        if new_cartesia_voice_id:
            logging.warning(
                f"DB save failed for localized voice {new_cartesia_voice_id} due to integrity error. Attempting Cartesia cleanup.",
            )
            try:
                cartesia_service.delete_voice(new_cartesia_voice_id)
            except Exception as e_cartesia_cleanup:
                logging.error(
                    f"Failed to cleanup Cartesia voice {new_cartesia_voice_id} after DB integrity error: {e_cartesia_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Failed to save localized voice to database, voice ID might already exist: {str(e_db_integrity)}",
        )
    except Exception as e_generic:
        session.rollback()
        if new_cartesia_voice_id:
            logging.warning(
                f"DB operation failed for localized voice {new_cartesia_voice_id}. Attempting Cartesia cleanup. Error: {str(e_generic)}",
            )
            try:
                cartesia_service.delete_voice(new_cartesia_voice_id)
            except Exception as e_cartesia_cleanup:
                logging.error(
                    f"Failed to cleanup Cartesia voice {new_cartesia_voice_id} after generic DB error: {e_cartesia_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to localize and save voice: {str(e_generic)}",
        )


@router.get(
    "/assistant/voice",
    response_model=InfoResponse[List[VoiceRead]],
    status_code=status.HTTP_200_OK,
    summary="List all voices for the user and global presets",
    description="Returns a list of all voices created or registered by the user, and all globally registered preset voices.",
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
                    is_preset=voice.is_preset,
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
    summary="Delete a user's voice record and from Cartesia if applicable",
    description="Deletes a specific voice record by its Cartesia ID for the authenticated user. If the voice is not a preset, it will also be deleted from Cartesia.",
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
    cartesia_service: CartesiaService = Depends(),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)

    try:
        voice_to_delete = voice_dao.get_voice_by_id(user_id=user_id, voice_id=voice_id)

        if not voice_to_delete:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Voice not found for this user.",
            )

        # Only attempt to delete from Cartesia if it's NOT a preset
        if not voice_to_delete.is_preset:
            try:
                cartesia_service.delete_voice(voice_id)
                logging.info(
                    f"Successfully deleted voice {voice_id} from Cartesia for user {user_id}.",
                )
            except CartesiaAPIError as e_cartesia:
                if e_cartesia.status_code == 404:
                    logging.warning(
                        f"Voice {voice_id} not found on Cartesia for user {user_id}. Proceeding with DB deletion.",
                    )
                else:
                    # For other Cartesia errors, prevent DB deletion and report
                    session.rollback()  # Rollback before raising to ensure no partial commit from earlier ops in this session
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,  # Or e_cartesia.status_code if appropriate
                        detail=f"Failed to delete voice from Cartesia: {e_cartesia.detail}",
                    )
            except (
                Exception
            ) as e_cartesia_generic:  # Catch any other unexpected error from service
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Unexpected error during Cartesia deletion: {str(e_cartesia_generic)}",
                )

        # If it was a preset, or Cartesia deletion was successful/404 for a non-preset
        voice_dao.delete_voice(
            user_id=user_id,
            voice_id=voice_id,
        )  # This re-fetches and deletes the user-specific record
        session.commit()
        return InfoResponse(info="Voice deleted successfully.")

    except HTTPException as e:
        session.rollback()
        raise e
    except Exception as e_generic_delete:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting voice record: {str(e_generic_delete)}",
        )


@router.post(
    "/assistant/photo/upload",
    response_model=InfoResponse[AssistantPhotoUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload an assistant profile photo",
    description="Uploads a profile photo for an assistant to GCS and returns the GCS URL.",
    tags=["Assistants", "Storage"],
)
async def upload_assistant_photo(
    request: Request,
    file: UploadFile = File(..., example="assistant_photo.jpg"),
):
    bucket_service = BucketService()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only images are allowed.",
        )

    MAX_SIZE_BYTES = 5 * 1024 * 1024
    if (
        file.size and file.size > MAX_SIZE_BYTES
    ):  # FastAPI's UploadFile might have size after spooling
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
        )

    try:
        file_content = await file.read()
        if len(file_content) > MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File content size exceeds {MAX_SIZE_BYTES // (1024*1024)}MB limit.",
            )

        gcs_url = bucket_service.upload_assistant_photo_file(
            file_content=file_content,
            user_id=user_id,
            content_type=file.content_type,
        )
        return InfoResponse(info=AssistantPhotoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error uploading assistant photo for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not upload photo: {str(e)}",
        )


@router.post(
    "/assistant/photo/generate",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Generate an assistant profile photo from text",
    description="Generates a new photo using a text prompt via Replicate and returns the image URL. This action will deduct credits.",
    tags=["Assistants", "Storage"],
)
def generate_assistant_photo(
    request: Request,
    payload: PhotoGenerateRequest,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
) -> InfoResponse[str]:
    """
    Generate a new assistant profile photo from a text prompt.

    This endpoint uses an AI model to generate an image based on the provided
    text prompt. The user's account is charged for this operation.
    """
    user_id = request.state.user_id
    users_dao = UsersDAO(session)

    # Pre-check credits if not in staging
    if not settings.is_staging:
        user = users_dao.get_user_with_id(user_id)
        if user.credits < settings.photo_generation_cost:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient credits to generate a photo.",
            )

    try:
        image_url = replicate_service.generate_photo(
            prompt=payload.prompt,
            aspect_ratio=payload.aspect_ratio,
            output_format=payload.output_format,
            output_quality=payload.output_quality,
            safety_tolerance=payload.safety_tolerance,
            prompt_upsampling=payload.prompt_upsampling,
        )

        # Deduct credits after successful generation if not in staging
        if not settings.is_staging:
            users_dao.recharge_credit(
                user_id=user_id,
                quantity=-float(settings.photo_generation_cost),
            )
            session.commit()

        return InfoResponse(info=image_url)
    except ReplicateAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except Exception as e:
        session.rollback()
        logging.error(f"Error generating photo for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not generate photo: {str(e)}",
        )


@router.post(
    "/assistant/photo/edit",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Edit an assistant profile photo from text",
    description="Edits a photo using a text prompt and an input image (URL or file) via Replicate, and returns the new image URL. This action will deduct credits.",
    tags=["Assistants", "Storage"],
)
async def edit_assistant_photo(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service: BucketService = Depends(),
    prompt: str = Form(...),
    input_image_url: Optional[str] = Form(None),
    input_image_file: Optional[UploadFile] = File(None),
    aspect_ratio: str = Form("match_input_image"),
    output_format: str = Form("jpg"),
    safety_tolerance: float = Form(2.0),
) -> InfoResponse[str]:
    """
    Edit an assistant profile photo using a text prompt and an input image.

    This endpoint uses an AI model to edit an existing image based on a
    text prompt. The input image can be provided as a public URL or a direct file upload.
    The user's account is charged for this operation.
    """
    user_id = request.state.user_id
    users_dao = UsersDAO(session)
    temp_gcs_url_to_delete: Optional[str] = None
    input_image_for_replicate: Optional[str] = None

    # A real file is provided if the UploadFile object exists and has a filename.
    # Test clients can send an empty file part which creates an object without a filename.
    is_file_provided = input_image_file and input_image_file.filename

    if (input_image_url and is_file_provided) or (
        not input_image_url and not is_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'input_image_url' or 'input_image_file', but not both.",
        )

    try:
        if is_file_provided:
            if (
                not input_image_file.content_type
                or not input_image_file.content_type.startswith(
                    "image/",
                )
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'input_image_file'. Only images are allowed.",
                )
            file_content = await input_image_file.read()
            (
                public_url,
                gcs_url_for_delete,
            ) = bucket_service.upload_temp_assistant_photo_file(
                file_content,
                user_id,
                input_image_file.content_type,
            )
            input_image_for_replicate = public_url
            temp_gcs_url_to_delete = gcs_url_for_delete
        else:
            input_image_for_replicate = input_image_url

        if not input_image_for_replicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid image input provided.",
            )

        # Pre-check credits if not in staging
        if not settings.is_staging:
            user = users_dao.get_user_with_id(user_id)
            if user.credits < settings.photo_generation_cost:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to edit a photo.",
                )

        image_url = replicate_service.edit_photo(
            prompt=prompt,
            input_image=input_image_for_replicate,
            aspect_ratio=aspect_ratio,
            output_format=output_format,
            safety_tolerance=safety_tolerance,
        )

        # Deduct credits after successful edit if not in staging
        if not settings.is_staging:
            users_dao.recharge_credit(
                user_id=user_id,
                quantity=-float(settings.photo_generation_cost),
            )
            session.commit()

        return InfoResponse(info=image_url)

    except ReplicateAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except Exception as e:
        session.rollback()
        logging.error(f"Error editing photo for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not edit photo: {str(e)}",
        )
    finally:
        if temp_gcs_url_to_delete:
            try:
                bucket_service.delete_assistant_photo(temp_gcs_url_to_delete)
                logging.info(
                    f"Successfully deleted temporary file {temp_gcs_url_to_delete} for photo edit.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary file {temp_gcs_url_to_delete}: {e_cleanup}",
                )


@router.post(
    "/assistant/video/animate",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Generate an animated video from image and audio",
    description="Generates an animated video using an input image and audio via Replicate. Inputs can be URLs or file uploads. This action will deduct credits.",
    tags=["Assistants", "Storage", "Video"],
)
async def animate_video_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service: BucketService = Depends(),
    image_url: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    audio_url: Optional[str] = Form(None),
    audio_file: Optional[UploadFile] = File(None),
    seed: Optional[int] = Form(None),
    dynamic_scale: Optional[float] = Form(1.0),
    min_resolution: Optional[int] = Form(512),
    inference_steps: Optional[int] = Form(25),
    keep_resolution: Optional[bool] = Form(True),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    users_dao = UsersDAO(session)

    temp_image_gcs_url: Optional[str] = None
    final_image_url_for_replicate: Optional[str] = None
    temp_audio_gcs_url: Optional[str] = None
    final_audio_url_for_replicate: Optional[str] = None

    is_image_file_provided = image_file and image_file.filename
    is_audio_file_provided = audio_file and audio_file.filename

    # Validate image input
    if (image_url and is_image_file_provided) or (
        not image_url and not is_image_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'image_url' or 'image_file', but not both.",
        )

    # Validate audio input
    if (audio_url and is_audio_file_provided) or (
        not audio_url and not is_audio_file_provided
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'audio_url' or 'audio_file', but not both.",
        )

    try:
        # Process image input
        if is_image_file_provided:
            if not image_file.content_type or not image_file.content_type.startswith(
                "image/",
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'image_file'. Only images are allowed.",
                )
            image_content = await image_file.read()
            (
                public_img_url,
                gcs_img_url,
            ) = bucket_service.upload_temp_assistant_photo_file(
                image_content,
                user_id,
                image_file.content_type,
            )
            final_image_url_for_replicate = public_img_url
            temp_image_gcs_url = gcs_img_url
        else:
            final_image_url_for_replicate = image_url

        # Process audio input
        if is_audio_file_provided:
            if not audio_file.content_type or not audio_file.content_type.startswith(
                "audio/",
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'audio_file'. Only audio files are allowed.",
                )
            audio_content = await audio_file.read()
            # Reusing upload_temp_assistant_photo_file for audio, path is generic enough
            (
                public_audio_url,
                gcs_audio_url,
            ) = bucket_service.upload_temp_assistant_photo_file(
                audio_content,
                user_id,
                audio_file.content_type,
            )
            final_audio_url_for_replicate = public_audio_url
            temp_audio_gcs_url = gcs_audio_url
        else:
            final_audio_url_for_replicate = audio_url

        if not final_image_url_for_replicate or not final_audio_url_for_replicate:
            # This case should be caught by earlier validation, but as a safeguard
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing valid image or audio input for Replicate.",
            )

        # Pre-check credits (assuming video_generation_cost is defined in settings)
        if not settings.is_staging:
            user = users_dao.get_user_with_id(user_id)
            if user.credits < settings.video_generation_cost:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to generate video.",
                )

        video_output_url = replicate_service.animate_video(
            image_url=final_image_url_for_replicate,
            audio_url=final_audio_url_for_replicate,
            seed=seed,
            dynamic_scale=dynamic_scale,
            min_resolution=min_resolution,
            inference_steps=inference_steps,
            keep_resolution=keep_resolution,
        )

        # Deduct credits after successful generation
        if not settings.is_staging:
            users_dao.recharge_credit(
                user_id=user_id,
                quantity=-float(settings.video_generation_cost),
            )
            session.commit()

        return InfoResponse(info=video_output_url)

    except ReplicateAPIError as e:
        session.rollback()
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except HTTPException: # Re-raise if it's already an HTTPException (e.g. from input validation)
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Error animating video for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not animate video: {str(e)}",
        )
    finally:
        # Cleanup temporary files from GCS
        if temp_image_gcs_url:
            try:
                bucket_service.delete_assistant_photo(temp_image_gcs_url)
                logging.info(
                    f"Successfully deleted temporary image file {temp_image_gcs_url} for video animation.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary image file {temp_image_gcs_url}: {e_cleanup}",
                )
        if temp_audio_gcs_url:
            try:
                bucket_service.delete_assistant_photo(temp_audio_gcs_url) # Reusing delete_assistant_photo
                logging.info(
                    f"Successfully deleted temporary audio file {temp_audio_gcs_url} for video animation.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary audio file {temp_audio_gcs_url}: {e_cleanup}",
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
