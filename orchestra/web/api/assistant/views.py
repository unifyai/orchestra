import base64
import logging
import time
from decimal import Decimal
from typing import List, Optional

import requests
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.recording_dao import RecordingDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Context
from orchestra.services.bucket_service import BucketService
from orchestra.services.call_recording_service import CallRecordingService
from orchestra.services.cartesia_service import CartesiaAPIError, CartesiaService
from orchestra.services.deepgram_service import DeepgramAPIError, DeepgramService
from orchestra.services.elevenlabs_service import ElevenLabsAPIError, ElevenLabsService
from orchestra.services.openai_service import OpenAIAPIError, OpenAIService
from orchestra.services.replicate_service import ReplicateAPIError, ReplicateService
from orchestra.settings import settings
from orchestra.web.api.assistant.schema import (
    AssistantCreate,
    AssistantPhotoUploadResponse,
    AssistantRead,
    AssistantStatus,
    AssistantUpdate,
    AssistantVideoUploadResponse,
    InfoResponse,
    PhotoGenerateRequest,
    RecordingCreate,
    RecordingInfo,
    VoiceCreate,
    VoiceDesignCreateFromPreviewRequest,
    VoiceDesignGeneratePreviewsAPIResponse,
    VoiceDesignGeneratePreviewsRequest,
    VoiceGenerateRequest,
    VoiceRead,
)
from orchestra.web.api.utils.assistant_infra import (
    assign_whatsapp_sender,
    create_email,
    create_phone_number,
    create_pubsub_topic,
    delete_email,
    delete_phone_number,
    delete_pubsub_topic,
    get_social_platforms_costs,
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
    tags=["Assistant Management"],
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
                            "country": "US",
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
        409: {
            "description": "Conflict",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "An assistant with the name 'Alice Smith' already exists for this user.",
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
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    log_event_dao = LogEventDAO(session)
    log_dao = LogDAO(session, context_dao)
    api_keys = api_key_dao.filter(user_id=user_id)
    if not api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized. Please contact support to get an API key.",
        )
    assistant = None

    # Determine total cost as base creation cost
    # plus premium for each social account added
    total_creation_cost = settings.assistant_creation_cost
    if assistant_in.user_whatsapp_number:
        try:
            platforms_response = get_social_platforms_costs()
            platforms = platforms_response.get("platforms")

            if not isinstance(platforms, dict):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Could not parse social platform costs. Expected a dictionary, got: {platforms}",
                )
            whatsapp_cost = platforms.get("whatsapp")
            if whatsapp_cost is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="WhatsApp cost not found in social platform costs response.",
                )
            total_creation_cost += Decimal(whatsapp_cost)
        except Exception as e_costs:

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch or process social platform costs. Details: {str(e_costs)}",
            )

    # Phase 1: Pre-checks and prepare assistant data
    try:
        ASSISTANTS_PROJECT_NAME = "Assistants"
        assistants_project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=ASSISTANTS_PROJECT_NAME,
        )
        if not assistants_project:
            project_dao.create(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
                description="Project to manage and track all assistants.",
                is_versioned=False,
            )
            # Re-fetch the project to get the object
            assistants_project = project_dao.get_by_user_and_name(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
            )

        if not settings.is_staging:
            user = users_dao.get_user_with_id(user_id)

            if user.credits < total_creation_cost:
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
            profile_video=assistant_in.profile_video,
            about=assistant_in.about,
            weekly_limit=parsed_weekly_limit,
            max_parallel=assistant_in.max_parallel,
            voice_id=assistant_in.voice_id,
            phone=assistant_in.phone,
            email=assistant_in.email,
            country=assistant_in.country,
            user_whatsapp_number=assistant_in.user_whatsapp_number,
        )

        # Commit the assistant creation before infrastructure setup
        # This ensures the assistant persists even if we refresh the session later
        session.commit()

        # Log pre-hire chat if provided
        if assistant_in.pre_hire_chat:
            try:
                context_name = f"{assistant.first_name}{assistant.surname}/Transcripts"
                chat_context_id = context_dao.get_or_create(
                    assistants_project.id,
                    name=context_name,
                )
                chat_context_obj = session.get(Context, chat_context_id)

                # Prepare entries for logging using jsonable_encoder
                chat_entries = jsonable_encoder(assistant_in.pre_hire_chat)
                num_entries = len(chat_entries)

                if num_entries > 0:
                    log_event_ids = log_event_dao.bulk_create(
                        project_id=assistants_project.id,
                        count=num_entries,
                        context_id=chat_context_id,
                    )

                    # Prepare all log rows for bulk creation
                    log_rows_to_create = []
                    for i, entry_dict in enumerate(chat_entries):
                        log_event_id = log_event_ids[i]
                        for key, value in entry_dict.items():
                            log_rows_to_create.append(
                                {
                                    "project_id": assistants_project.id,
                                    "log_event_id": log_event_id,
                                    "key": key,
                                    "value": value,
                                    "context_id": chat_context_id,
                                },
                            )

                    # Bulk create the log rows (this will flush)
                    if log_rows_to_create:
                        log_dao.bulk_create(
                            log_rows_to_create,
                            context_obj=chat_context_obj,
                        )

                    session.commit()  # Commit the logs

            except Exception as e_log:
                session.rollback()  # Rollback the log transaction
                logging.warning(
                    f"Failed to log pre-hire chat for assistant {assistant.agent_id}. Error: {str(e_log)}",
                )

        assistant_id = assistant.agent_id
        # Infrastructure creation with rollback on failure
        created_email = None
        created_phone = None
        created_pubsub = None
        assigned_whatsapp = None

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
                country = assistant_in.country if assistant_in.country else "US"
                phone_response = create_phone_number(
                    country=country,
                    is_staging=settings.is_staging,
                )
                if "detail" in phone_response:
                    raise Exception(
                        f"Phone number creation failed: {phone_response['detail']}",
                    )
                created_phone = phone_response.get("phoneNumber")
                print(f"PHONE CREATED: {created_phone}")

                # Step 4: assign whatsapp sender if whatsapp number is provided
                if assistant_in.user_whatsapp_number:
                    assigned_whatsapp = assign_whatsapp_sender(
                        assistant_in.user_whatsapp_number,
                        is_staging=settings.is_staging,
                    )["whatsapp_number"]

                # Step 5: create pubsub topic
                pubsub_response = create_pubsub_topic(
                    str(assistant_id),
                    is_staging=settings.is_staging,
                )
                if "detail" in pubsub_response:
                    raise Exception(
                        f"Pubsub topic creation failed: {pubsub_response['detail']}",
                    )
                created_pubsub = True
                print(f"PUBSUB CREATED: {assistant_id}")

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
                    user_whatsapp_number=assistant_in.user_whatsapp_number,
                    assistant_whatsapp_number=assigned_whatsapp,
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
                print(f"INFRA ERROR: {infra_error}")

                # can't rollback infra if the setup isn't complete so need to wait
                time.sleep(10)

                # Refresh database session to avoid stale connections during rollback
                logging.warning(
                    f"Infrastructure setup failed for assistant {assistant_id}, refreshing session for rollback",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)
                context_dao = ContextDAO(session)
                project_dao = ProjectDAO(
                    session,
                    organization_member_dao,
                    context_dao,
                )

                # Rollback infrastructure in reverse order
                rollback_errors = []

                if created_pubsub:
                    try:
                        delete_pubsub_topic(
                            str(assistant_id),
                            is_staging=settings.is_staging,
                        )
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
                    # First, delete the chat context if it was created
                    if assistant_in.pre_hire_chat:
                        try:
                            context_name = f"{assistant_in.first_name}{assistant_in.surname}/Transcripts"
                            assistants_project = project_dao.get_by_user_and_name(
                                user_id=user_id,
                                name="Assistants",
                            )
                            if assistants_project:
                                context_to_delete = context_dao.filter(
                                    project_id=assistants_project.id,
                                    name=context_name,
                                )
                                if context_to_delete:
                                    context_dao.delete(context_to_delete[0][0].id)
                                    logging.info(
                                        f"Deleted chat transcript context for failed assistant {assistant_id}",
                                    )
                        except Exception as e_ctx_del:
                            rollback_errors.append(
                                f"Failed to delete chat context: {str(e_ctx_del)}",
                            )
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

    except IntegrityError as e:
        session.rollback()
        if "uq_user_assistant_name" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An assistant with the name '{assistant_in.first_name} {assistant_in.surname}' already exists for this user.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database error creating assistant: {str(e)}",
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
                quantity=-float(total_creation_cost),
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
            user_id=assistant.user_id,
            first_name=assistant.first_name,
            surname=assistant.surname,
            age=assistant.age,
            region=assistant.region,
            profile_photo=assistant.profile_photo,
            profile_video=assistant.profile_video,
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
            voice_id=assistant.voice_id,
            country=assistant.country,
            user_whatsapp_number=assistant.user_whatsapp_number,
            assistant_whatsapp_number=assistant.assistant_whatsapp_number,
            user_phone=assistant.user_phone,
        ),
    )


@router.get(
    "/assistant",
    response_model=InfoResponse[List[AssistantRead]],
    status_code=status.HTTP_200_OK,
    summary="List all assistants",
    description="Returns a list of all assistants belonging to the authenticated user.",
    tags=["Assistant Management"],
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
                                "profile_video": "https://example.com/videos/alice.mp4",
                                "about": "Mathematician and writer known for work on Analytical Engine",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "country": "US",
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
                                "profile_video": "https://example.com/videos/bob.mp4",
                                "about": "Machine learning expert with focus on computer vision",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "country": "CA",
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

    assistant_dao = AssistantDAO(session)
    try:
        assistants = assistant_dao.list_assistants_for_user(
            request.state.user_id,
            phone=phone,
            email=email,
        )
        voice_dao = VoiceDAO(session)
        tts_providers = [
            (
                voice_dao.get_voice_by_id(a.user_id, a.voice_id).provider
                if a.voice_id is not None
                else "cartesia"
            )
            for a in assistants
        ]
        return InfoResponse(
            info=[
                AssistantRead(
                    agent_id=str(a.agent_id),
                    user_id=a.user_id,
                    first_name=a.first_name,
                    surname=a.surname,
                    age=a.age,
                    region=a.region,
                    profile_photo=a.profile_photo,
                    profile_video=a.profile_video,
                    about=a.about,
                    country=a.country,
                    weekly_limit=(
                        float(a.weekly_limit) if a.weekly_limit is not None else None
                    ),
                    max_parallel=a.max_parallel,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                    phone=a.phone,
                    user_phone=a.user_phone,
                    user_whatsapp_number=a.user_whatsapp_number,
                    assistant_whatsapp_number=a.assistant_whatsapp_number,
                    email=a.email,
                    tts_provider=tts_providers[i],
                    voice_id=a.voice_id,
                )
                for i, a in enumerate(assistants)
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
    tags=["Assistant Management"],
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
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    cleanup_errors = []
    try:
        # First get the assistant to retrieve infrastructure details including GCS photo URL
        assistant = dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
        )
        if not assistant:
            logging.warning(
                f"Assistant with ID {assistant_id} not found for user {request.state.user_id}.",
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # Delete the associated chat transcript context from the "Assistants" project
        try:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            assistants_project = project_dao.get_by_user_and_name(
                user_id=request.state.user_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            if assistants_project:
                assistant_context_prefix = f"{assistant.first_name}{assistant.surname}"
                # Find all contexts related to the assistant (e.g., "AdaLovelace", "AdaLovelace/Transcripts")
                contexts_to_delete = (
                    session.query(Context)
                    .filter(
                        Context.project_id == assistants_project.id,
                        or_(
                            Context.name == assistant_context_prefix,
                            Context.name.like(f"{assistant_context_prefix}/%"),
                        ),
                    )
                    .all()
                )

                if contexts_to_delete:
                    for context_to_del in contexts_to_delete:
                        context_dao.delete(context_to_del.id)

        except Exception as e_ctx:
            logging.error(
                f"Failed to stage context deletion for assistant {assistant_id}: {str(e_ctx)}",
            )
            cleanup_errors.append(
                f"Failed to delete assistant context(s): {str(e_ctx)}",
            )

        # Delete GCS profile photo if it exists and is a GCS URL from the assistant images bucket
        if assistant.profile_photo and assistant.profile_photo.startswith("gs://"):
            try:
                deleted_from_gcs = bucket_service.delete_assistant_file(
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

        # Delete GCS profile video if it exists
        if assistant.profile_video and assistant.profile_video.startswith("gs://"):
            try:
                deleted_from_gcs = bucket_service.delete_assistant_file(
                    assistant.profile_video,
                )
                if not deleted_from_gcs:
                    logging.error(
                        f"Profile video {assistant.profile_video} for assistant {assistant_id} was not deleted from GCS (either not found, wrong bucket, or other non-critical issue).",
                    )
                    cleanup_errors.append(
                        f"Failed to delete profile video: {str(e_gcs)}",
                    )
            except Exception as e_gcs:
                logging.error(
                    f"Failed to delete profile video {assistant.profile_video} for assistant {assistant_id}: {str(e_gcs)}",
                )
                cleanup_errors.append(f"Failed to delete profile video: {str(e_gcs)}")

        # Wait before starting other infra cleanup (same as rollback operations)
        time.sleep(10)

        # Delete pubsub topic
        try:
            delete_pubsub_topic(str(assistant_id), is_staging=settings.is_staging)
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

        # Commit the entire transaction
        session.commit()

        response_msg = "Assistant deleted successfully"
        if cleanup_errors:
            response_msg += f" (with some cleanup issues: {'; '.join(cleanup_errors)})"

        return InfoResponse(info=response_msg)
    except HTTPException:
        logging.warning(
            f"Rolling back transaction due to HTTPException during deletion of assistant {assistant_id}.",
        )
        session.rollback()
        raise
    except Exception as e:
        logging.error(
            f"An unexpected error occurred during deletion of assistant {assistant_id}. Rolling back.",
            exc_info=True,
        )
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
    tags=["Assistant Management"],
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
                            "profile_video": "https://example.com/videos/alice.mp4",
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "country": "US",
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
    user_id = request.state.user_id
    users_dao = UsersDAO(session)
    assistant_dao = AssistantDAO(session)
    bucket_service = BucketService()

    # Store the old photo URL before the update
    old_photo_url = None
    is_photo_changing = False
    old_video_url = None
    is_video_changing = False

    # Check assistant existence before any updates
    existing_assistant = assistant_dao.get_assistant_by_id(
        user_id=request.state.user_id,
        agent_id=assistant_id,
    )
    if not existing_assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # Determine if the photo is being updated before making changes
    old_photo_url = existing_assistant.profile_photo
    is_photo_changing = (
        update.profile_photo is not None and update.profile_photo != old_photo_url
    )
    old_video_url = existing_assistant.profile_video
    is_video_changing = (
        update.profile_video is not None and update.profile_video != old_video_url
    )

    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        # Create / update social account:
        # 1- Check if the assistant doesn't have a user account already and if a user account value is provided
        # 2- If so and if user has enough credits (production), assign the whatsapp account to the assistant
        assistant_whatsapp_number = (
            existing_assistant.assistant_whatsapp_number
            if existing_assistant.assistant_whatsapp_number
            else None
        )
        if update.user_whatsapp_number and not existing_assistant.user_whatsapp_number:
            if not settings.is_staging:
                # Cost to create a social account
                try:
                    platforms_response = get_social_platforms_costs()
                    platforms = platforms_response.get("platforms")

                    if not isinstance(platforms, dict):
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Could not parse social platform costs. Expected a dictionary, got: {platforms}",
                        )
                    cost = platforms.get("whatsapp")
                    if cost is None:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="WhatsApp cost not found in social platform costs response.",
                        )
                except Exception as e_costs:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to fetch or process social platform costs. Details: {str(e_costs)}",
                    )
                user = users_dao.get_user_with_id(user_id)
                decimal_cost = Decimal(cost)
                if user.credits < decimal_cost:
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail="Insufficient credits to add a WhatsApp number.",
                    )
                users_dao.recharge_credit(
                    user_id=user_id,
                    quantity=-float(decimal_cost),
                )

            assistant_whatsapp_number = assign_whatsapp_sender(
                update.user_whatsapp_number,
                is_staging=settings.is_staging,
            )["whatsapp_number"]

        updated = assistant_dao.update_assistant(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            profile_photo=update.profile_photo,
            profile_video=update.profile_video,
            about=update.about,
            phone=update.phone,
            email=update.email,
            user_phone=update.user_phone,
            user_whatsapp_number=update.user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
            weekly_limit=weekly_limit,
            max_parallel=update.max_parallel,
            voice_id=update.voice_id,
            country=update.country,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # If the photo was updated, delete the old one from GCS.
        if is_photo_changing and old_photo_url and old_photo_url.startswith("gs://"):
            try:
                bucket_service.delete_assistant_file(old_photo_url)
                logging.info(
                    f"Successfully deleted old profile photo {old_photo_url} for assistant {assistant_id}.",
                )
            except Exception as e:
                logging.error(
                    f"Failed to delete old profile photo {old_photo_url} for assistant {assistant_id} during update. Error: {str(e)}",
                )

        # If the video was updated, delete the old one from GCS.
        if is_video_changing and old_video_url and old_video_url.startswith("gs://"):
            try:
                bucket_service.delete_assistant_file(old_video_url)
                logging.info(
                    f"Successfully deleted old profile video {old_video_url} for assistant {assistant_id}.",
                )
            except Exception as e:
                logging.error(
                    f"Failed to delete old profile video {old_video_url} for assistant {assistant_id} during update. Error: {str(e)}",
                )

        return InfoResponse(
            info=AssistantRead(
                agent_id=str(updated.agent_id),
                user_id=updated.user_id,
                first_name=updated.first_name,
                surname=updated.surname,
                age=updated.age,
                region=updated.region,
                profile_photo=updated.profile_photo,
                profile_video=updated.profile_video,
                about=updated.about,
                country=updated.country,
                weekly_limit=float(updated.weekly_limit),
                max_parallel=updated.max_parallel,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
                phone=updated.phone,
                email=updated.email,
                user_whatsapp_number=updated.user_whatsapp_number,
                assistant_whatsapp_number=assistant_whatsapp_number,
                user_phone=updated.user_phone,
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


@admin_router.post(
    "/assistant/recordings",
    response_model=InfoResponse[RecordingInfo],
    status_code=status.HTTP_200_OK,
    summary="Add a call recording for an assistant",
    description="Uploads a new call recording for the specified assistant.",
    tags=["Recordings"],
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
    recording: RecordingCreate,
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
            user_id=recording.user_id,
            agent_id=recording.assistant_id,
            recording_raw=recording.recording_raw,
            content_type=mime,
            is_staging=settings.is_staging,
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
    tags=["Recordings"],
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
    summary="Register voice",
    description="Register a preset assistant voice.",
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
                            "provider": "cartesia",
                            "is_preset": True,
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
def register_voice(
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
            provider=voice_in.provider,
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
                provider=voice.provider,
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
                detail=f"Voice with ID '{voice_in.voice_id}' already exists for this user.",
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
    summary="Clone voice",
    description="Create a new assistant voice by cloning a voice from an audio file.",
    tags=["Voices"],
)
async def clone_voice(
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
    deepgram_service: DeepgramService = Depends(),
    name: str = Form(..., example="My Voice Clone"),
    language: Optional[str] = Form(None, example="en"),
    description: Optional[str] = Form(None, example="A cloned voice for my assistant"),
    gender: Optional[str] = Form(None, example="female"),
    provider: str = Form("cartesia"),
    file: UploadFile = File(..., example="voice_sample.wav"),
):
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_voice_id: Optional[str] = None
    voice_language: Optional[str] = language

    try:
        file_content = await file.read()
        if not voice_language:
            try:
                detected_language = deepgram_service.detect_language_from_audio(
                    file_content,
                    user_id,
                    file.content_type,
                )
                voice_language = detected_language or "en"
            except DeepgramAPIError as e:
                logging.error(
                    f"Deepgram API error during voice clone language detection: {e.detail}",
                )
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"Language detection failed: {e.detail}",
                )

        if provider == "cartesia":
            cartesia_response = cartesia_service.clone_voice(
                file_content=file_content,
                file_name=file.filename or "audio_clip_default_name",
                name=name,
                language=voice_language,
                description=description,
            )
            new_voice_id = cartesia_response.get("id")
        elif provider == "elevenlabs":
            elevenlabs_response = elevenlabs_service.clone_voice(
                file_content=file_content,
                file_name=file.filename or "audio_clip_default_name",
                name=name,
                description=description,
            )
            new_voice_id = elevenlabs_response.get("voice_id")
        else:
            raise HTTPException(status_code=400, detail="Invalid provider.")

        if not new_voice_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{provider.capitalize()} did not return a voice ID after cloning.",
            )

        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_voice_id,
            name=name,
            description=description or f"Cloned voice: {name}",
            gender=gender,
            language=voice_language,
            provider=provider,
        )
        if provider == "cartesia" and not gender:
            db_voice.gender = cartesia_response.get("gender")
        db_voice.is_preset = False
        session.commit()

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                provider=db_voice.provider,
                is_preset=False,
            ),
        )

    except (CartesiaAPIError, ElevenLabsAPIError, DeepgramAPIError) as e:
        session.rollback()
        service_name = "External service"
        if isinstance(e, CartesiaAPIError):
            service_name = "Cartesia"
        elif isinstance(e, ElevenLabsAPIError):
            service_name = "ElevenLabs"
        elif isinstance(e, DeepgramAPIError):
            service_name = "Language Detection"
        raise HTTPException(
            status_code=e.status_code,
            detail=f"{service_name} API error: {e.detail}",
        )
    except IntegrityError as e_db_integrity:
        session.rollback()
        if new_voice_id:
            logging.warning(
                f"DB save failed for cloned voice {new_voice_id} due to integrity error. Attempting {provider} cleanup.",
            )
            if provider == "cartesia":
                provider_service = cartesia_service
            elif provider == "elevenlabs":
                provider_service = elevenlabs_service
            try:
                provider_service.delete_voice(new_voice_id)
            except Exception as e_voice_cleanup:
                logging.error(
                    f"Failed to cleanup {provider} voice {new_voice_id} after DB integrity error: {e_voice_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Failed to save cloned voice to database, voice ID might already exist: {str(e_db_integrity)}",
        )
    except Exception as e_generic:
        session.rollback()
        if new_voice_id:
            if provider == "cartesia":
                provider_service = cartesia_service
            elif provider == "elevenlabs":
                provider_service = elevenlabs_service
            try:
                cartesia_service.delete_voice(new_voice_id)
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clone and save voice: {str(e_generic)}",
        )


@router.get(
    "/assistant/voice",
    response_model=InfoResponse[List[VoiceRead]],
    status_code=status.HTTP_200_OK,
    summary="List voices",
    description="Returns a list of all assistant voices created for the user.",
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
                                "provider": "cartesia",
                                "is_preset": True,
                            },
                            {
                                "voice_id": "c99d36f3-5ffd-4253-803a-535c1bc9c306",
                                "name": "English Male Deep 1",
                                "description": "A deep, smoooth British man's voice perfect for narration.",
                                "gender": "male",
                                "language": "en",
                                "provider": "elevenlabs",
                                "is_preset": False,
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
                    provider=voice.provider,
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
    summary="Delete voice",
    description="Deletes a specific assistant voice.",
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
    elevenlabs_service: ElevenLabsService = Depends(),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)

    # Step 1: Get the voice from DB
    voice_to_delete = voice_dao.get_voice_by_id(user_id=user_id, voice_id=voice_id)
    if not voice_to_delete:
        # No session.rollback() needed here as it's a read operation that failed to find.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice not found for this user.",
        )

    # Step 2: Attempt to delete from provider if applicable
    if not voice_to_delete.is_preset:

        provider_service = None
        if voice_to_delete.provider == "cartesia":
            provider_service = cartesia_service
        elif voice_to_delete.provider == "elevenlabs":
            provider_service = elevenlabs_service

        if provider_service:
            try:
                provider_service.delete_voice(voice_id)
            except (CartesiaAPIError, ElevenLabsAPIError) as e_provider:
                if e_provider.status_code == 404:
                    logging.warning(
                        f"Voice {voice_id} not found on {voice_to_delete.provider} (status 404). Proceeding with DB deletion.",
                    )
                    # Non-critical, continue to DB deletion
                else:
                    # CRITICAL PROVIDER FAILURE
                    logging.error(
                        f"Critical error deleting voice {voice_id} from {voice_to_delete.provider}: {e_provider.detail}",
                    )
                    session.rollback()  # Ensure rollback of any prior DB changes in this session (though unlikely here)
                    raise HTTPException(
                        status_code=e_provider.status_code,
                        detail=f"Failed to delete voice from {voice_to_delete.provider}: {e_provider.detail}",
                    )
            except Exception as e_provider_generic:
                # Other unexpected provider errors
                logging.error(
                    f"Unexpected critical error deleting voice {voice_id} from {voice_to_delete.provider}: {str(e_provider_generic)}",
                )
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Unexpected error during {voice_to_delete.provider} deletion: {str(e_provider_generic)}",
                )

    # Step 3: If we've reached here, it means:
    # - Voice is a preset (provider deletion skipped)
    # - OR Provider deletion was successful
    # - OR Provider deletion returned 404 (non-critical)
    # So, proceed to delete from our database.
    try:
        voice_dao.delete_voice(user_id=user_id, voice_id=voice_id)
        session.commit()
        return InfoResponse(info="Voice deleted successfully.")
    except (
        IntegrityError
    ) as e_db_integrity:  # Should not happen on delete typically, but good to catch
        session.rollback()
        logging.error(
            f"DB IntegrityError during voice deletion from DB {voice_id}: {str(e_db_integrity)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database integrity error during voice deletion: {str(e_db_integrity)}",
        )
    except Exception as e_db_generic:  # Other errors during DB delete
        session.rollback()
        logging.error(
            f"Generic error during voice deletion from DB {voice_id}: {str(e_db_generic)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting voice from database: {str(e_db_generic)}",
        )


@router.post(
    "/assistant/voice/generate",
    # response_model is not InfoResponse[bytes] because we return raw audio
    status_code=status.HTTP_200_OK,
    summary="Generate speech from text",
    description="Generates audio from text using the specified provider and voice.",
    tags=["Voices"],
    responses={
        200: {
            "description": "Audio generated successfully. Content-Type will be audio/mpeg, audio/wav, etc.",
            # "content" example not straightforward for raw bytes, will depend on format
        },
        400: {
            "description": "Bad Request (e.g., invalid provider, provider API error)",
            "content": {
                "application/json": {"example": {"detail": "Provider API error: ..."}},
            },
        },
        503: {
            "description": "Service unavailable (e.g. provider API down)",
            "content": {
                "application/json": {
                    "example": {"detail": "TTS provider unavailable."},
                },
            },
        },
    },
)
async def generate_speech(
    request_data: VoiceGenerateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
) -> Response:
    user_id = request.state.user_id
    audio_bytes: bytes
    content_type: str

    try:
        if request_data.provider == "cartesia":
            audio_bytes, content_type = cartesia_service.generate_speech(
                transcript=request_data.text,
                voice_id=request_data.voice_id,
                model_id=request_data.model_id or "sonic-2",  # Default Cartesia model
                output_format_container=request_data.output_format,
                output_sample_rate=request_data.cartesia_sample_rate,
                output_bit_rate=request_data.cartesia_bit_rate,
                language=request_data.cartesia_language,
            )
        elif request_data.provider == "elevenlabs":
            audio_bytes, content_type = elevenlabs_service.generate_speech(
                text=request_data.text,
                voice_id=request_data.voice_id,
                model_id=request_data.model_id
                or "eleven_multilingual_v2",  # Default EL model
                output_format=request_data.output_format,
                optimize_streaming_latency=request_data.elevenlabs_optimize_streaming_latency,
                stability=request_data.elevenlabs_voice_settings_stability,
                similarity_boost=request_data.elevenlabs_voice_settings_similarity_boost,
            )
        else:
            # This case should be prevented by Pydantic's Literal validation
            raise HTTPException(
                status_code=400,
                detail="Invalid TTS provider specified.",
            )

        return Response(content=audio_bytes, media_type=content_type)

    except (CartesiaAPIError, ElevenLabsAPIError) as e:
        logging.error(
            f"TTS API error for user {user_id}, provider {request_data.provider}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"TTS provider error: {e.detail}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Unexpected error generating speech for user {user_id}: {str(e)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate speech: {str(e)}",
        )


@router.post(
    "/assistant/voice/design/preview",
    response_model=InfoResponse[VoiceDesignGeneratePreviewsAPIResponse],
    status_code=status.HTTP_200_OK,
    summary="Design Voice Previews",
    description="Generates voice design previews from a text description.",
    tags=["Voices", "TTS Design"],
)
async def design_voice_generate_previews_endpoint(
    request_data: VoiceDesignGeneratePreviewsRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    elevenlabs_service: ElevenLabsService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[VoiceDesignGeneratePreviewsAPIResponse]:
    user_id = request.state.user_id
    final_voice_description = request_data.voice_description

    try:
        # If a bio is provided, use OpenAI to generate a more detailed description
        if request_data.bio:
            try:
                final_voice_description = (
                    openai_service.generate_voice_description_from_bio(
                        bio=request_data.bio,
                        description_hint=request_data.voice_description,
                    )
                )
                if not (20 <= len(final_voice_description) <= 1000):
                    logging.error(
                        f"OpenAI-generated voice description has invalid length ({len(final_voice_description)} chars). Content: '{final_voice_description}'",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to generate a voice description with the required length (20-1000 characters). Please try again.",
                    )
            except OpenAIAPIError as e:
                logging.error(
                    f"OpenAI API error during voice description generation: {e.detail}",
                )
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"Failed to generate voice description from bio: {e.detail}",
                )

        if not final_voice_description:
            # This should be caught by the pydantic validator, but as a safeguard.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A voice description is required. Provide 'voice_description' or 'bio'.",
            )
        el_response_data = elevenlabs_service.design_voice_generate_previews(
            voice_description=final_voice_description,
            text_for_preview=request_data.text,
            auto_generate_text_flag=request_data.auto_generate_text,
            model_id_for_design=request_data.model_id,
        )

        # Pydantic will validate if el_response_data matches VoiceDesignGeneratePreviewsAPIResponse
        return InfoResponse(
            info=VoiceDesignGeneratePreviewsAPIResponse(**el_response_data),
        )

    except ElevenLabsAPIError as e:
        logging.error(
            f"ElevenLabs voice design preview error for user {user_id}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"ElevenLabs API error: {e.detail}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Unexpected error generating voice previews for user {user_id}: {str(e)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate voice previews: {str(e)}",
        )


@router.post(
    "/assistant/voice/design/create",
    response_model=InfoResponse[VoiceRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create Voice from Design Preview",
    description="Creates a full voice from a generated preview voice id.",
    tags=["Voices", "TTS Design"],
)
async def design_voice_create_from_preview_endpoint(
    request_data: VoiceDesignCreateFromPreviewRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    elevenlabs_service: ElevenLabsService = Depends(),
    deepgram_service: DeepgramService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[VoiceRead]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)
    new_el_voice_id: Optional[str] = None
    voice_language: Optional[str] = request_data.language

    try:
        if not voice_language:
            # Prioritize language detection from audio if provided
            if request_data.audio_base_64:
                try:
                    audio_content = base64.b64decode(request_data.audio_base_64)
                    # Assume MP3 if media_type is not provided
                    media_type = request_data.media_type or "audio/mpeg"
                    detected_language = deepgram_service.detect_language_from_audio(
                        audio_content=audio_content,
                        user_id=user_id,
                        content_type=media_type,
                    )
                    voice_language = detected_language or "en"
                except DeepgramAPIError as e:
                    logging.error(
                        f"Deepgram API error during design/create language detection: {e.detail}",
                    )
                    raise HTTPException(
                        status_code=e.status_code,
                        detail=f"Language detection from audio failed: {e.detail}",
                    )
                except Exception as e_decode:
                    logging.error(
                        f"Failed to decode base64 audio for language detection: {str(e_decode)}",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid base64 audio data provided.",
                    )
            # Fallback to language detection from text description
            else:
                try:
                    detected_language = openai_service.detect_language_from_text(
                        request_data.voice_description,
                    )
                    voice_language = detected_language or "en"
                except OpenAIAPIError as e:
                    logging.error(
                        f"OpenAI API error during design/create language detection: {e.detail}",
                    )
                    raise HTTPException(
                        status_code=e.status_code,
                        detail=f"Language detection from text failed: {e.detail}",
                    )

        # Step 1: Call ElevenLabs to create the voice from the generated_voice_id
        el_created_voice_data = elevenlabs_service.create_voice_from_generated_id(
            voice_name=request_data.voice_name,
            generated_voice_id=request_data.generated_voice_id,
            description=request_data.voice_description,
            labels=request_data.labels,
        )

        new_el_voice_id = el_created_voice_data.get("voice_id")
        if not new_el_voice_id:
            raise ElevenLabsAPIError(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="ElevenLabs did not return a 'voice_id' after creating the voice from preview.",
            )

        # Step 2: Save the new voice to our database
        db_voice = voice_dao.create_voice(
            user_id=user_id,
            voice_id=new_el_voice_id,
            name=request_data.voice_name,
            description=request_data.voice_description
            or f"Designed voice: {request_data.voice_name}",
            language=voice_language,
            gender=request_data.gender,
            provider="elevenlabs",
        )
        db_voice.is_preset = False  # Designed voices are not presets
        session.flush()  # Ensure db_voice gets all attributes before commit
        session.commit()  # Commit DB voice creation

        return InfoResponse(
            info=VoiceRead(
                voice_id=db_voice.voice_id,
                name=db_voice.name,
                description=db_voice.description,
                language=db_voice.language,
                gender=db_voice.gender,
                provider=db_voice.provider,
                is_preset=db_voice.is_preset,
            ),
        )

    except (ElevenLabsAPIError, DeepgramAPIError, OpenAIAPIError) as e:
        session.rollback()
        service_name = "External service"
        should_cleanup_el = isinstance(e, ElevenLabsAPIError)

        if isinstance(e, ElevenLabsAPIError):
            service_name = "ElevenLabs"
        elif isinstance(e, (DeepgramAPIError, OpenAIAPIError)):
            service_name = "Language Detection"
            should_cleanup_el = False  # Don't cleanup if EL was never called

        if new_el_voice_id and should_cleanup_el:
            try:
                logging.warning(
                    f"Attempting to clean up orphaned ElevenLabs voice {new_el_voice_id} due to error: {e.detail}",
                )
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup orphaned ElevenLabs voice {new_el_voice_id}: {e_cleanup}",
                )
        logging.error(
            f"{service_name} error during voice creation from preview for user {user_id}: {e.detail}",
        )
        raise HTTPException(
            status_code=e.status_code,
            detail=f"{service_name} API error: {e.detail}",
        )
    except IntegrityError as e_db:
        session.rollback()
        if (
            new_el_voice_id
        ):  # EL voice was created, but DB failed (e.g. voice_id already exists in our DB by chance)
            logging.warning(
                f"DB IntegrityError for EL voice {new_el_voice_id}. Attempting EL cleanup.",
            )
            try:
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup EL voice {new_el_voice_id} after DB integrity error: {e_cleanup}",
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Database error creating voice, it might already exist: {str(e_db)}",
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if new_el_voice_id:  # EL voice might have been created
            logging.warning(
                f"Generic error after EL voice {new_el_voice_id} might have been created. Attempting EL cleanup.",
            )
            try:
                elevenlabs_service.delete_voice(new_el_voice_id)
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to cleanup EL voice {new_el_voice_id} after generic error: {e_cleanup}",
                )
        logging.error(
            f"Unexpected error creating voice from preview for user {user_id}: {str(e)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create voice from preview: {str(e)}",
        )


@router.post(
    "/assistant/photo/upload",
    response_model=InfoResponse[AssistantPhotoUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload photo",
    description="Uploads a profile photo for an assistant and return the storage URL.",
    tags=["Media"],
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
    "/assistant/video/upload",
    response_model=InfoResponse[AssistantVideoUploadResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Upload video",
    description="Uploads a profile video for an assistant and returns the storage URL.",
    tags=["Media"],
)
async def upload_assistant_video(
    request: Request,
    file: UploadFile = File(..., example="assistant_video.mp4"),
):
    bucket_service = BucketService()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only videos are allowed.",
        )

    MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50MB limit for videos
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
        return InfoResponse(info=AssistantVideoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error uploading assistant video for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not upload video: {str(e)}",
        )


@router.post(
    "/assistant/photo/generate",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Generate photo",
    description="Generates a new photo using a text prompt and returns the image URL. This action costs credits.",
    tags=["Media"],
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
    summary="Edit photo",
    description="Edits a photo using a text prompt and an input image (URL or file), and returns the image URL. This action costs credits.",
    tags=["Media"],
)
async def edit_assistant_photo(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service: BucketService = Depends(),
    prompt: str = Form(
        ...,
        example="A photo of a young woman with long brown hair and blue eyes.",
    ),
    input_image_url: Optional[str] = Form(
        None,
        example="https://example.com/input_image.jpg",
    ),
    input_image_file: Optional[UploadFile] = File(None, example="input_image.jpg"),
    aspect_ratio: str = Form("match_input_image", example="1:1"),
    output_format: str = Form("jpg", example="jpg"),
    safety_tolerance: float = Form(2.0, example=2.0),
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
            ) = bucket_service.upload_temp_assistant_file(
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
                bucket_service.delete_assistant_file(temp_gcs_url_to_delete)
                logging.info(
                    f"Successfully deleted temporary file {temp_gcs_url_to_delete} for photo edit.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary file {temp_gcs_url_to_delete}: {e_cleanup}",
                )


@router.post(
    "/assistant/photo/animate",
    response_model=InfoResponse[str],
    status_code=status.HTTP_201_CREATED,
    summary="Animate photo",
    description="Generates an animated video of the assistant using an input image and audio. Inputs can be URLs or file uploads. This action costs credits.",
    tags=["Media"],
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
            (public_img_url, gcs_img_url) = bucket_service.upload_temp_assistant_file(
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
            # Reusing upload_temp_assistant_file for audio, path is generic enough
            (
                public_audio_url,
                gcs_audio_url,
            ) = bucket_service.upload_temp_assistant_file(
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
    except (
        HTTPException
    ):  # Re-raise if it's already an HTTPException (e.g. from input validation)
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
                bucket_service.delete_assistant_file(temp_image_gcs_url)
                logging.info(
                    f"Successfully deleted temporary image file {temp_image_gcs_url} for video animation.",
                )
            except Exception as e_cleanup:
                logging.error(
                    f"Failed to clean up temporary image file {temp_image_gcs_url}: {e_cleanup}",
                )
        if temp_audio_gcs_url:
            try:
                bucket_service.delete_assistant_file(
                    temp_audio_gcs_url,
                )  # Reusing delete_assistant_file
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


@admin_router.get(
    "/assistant/{assistant_id}/status",
    response_model=InfoResponse[AssistantStatus],
    status_code=status.HTTP_200_OK,
    summary="Admin: Get assistant service status",
    description="Retrieves the live status of a specific assistant's running service. Prioritizes a configured admin key, but can fall back to the request's auth header.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "Assistant status retrieved successfully.",
        },
        404: {
            "description": "Assistant service not found or not responding.",
        },
        500: {
            "description": "Configuration or authorization error.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "ASSISTANT_ADMIN_KEY is not configured, and a valid Bearer token was not provided in the request header as a fallback.",
                    },
                },
            },
        },
        503: {
            "description": "Could not connect to the assistant service.",
        },
    },
)
def admin_get_assistant_status(
    assistant_id: str,
    request: Request,
) -> InfoResponse[AssistantStatus]:
    """
    Get the live status of an assistant's dedicated service.
    """

    # Prioritize the key from settings if unity admin key
    # needs to be different from the orchestra admin key.
    # Otherwise use the key provided in the auth header.
    auth_header = None
    if settings.UNITY_ADMIN_KEY:
        auth_header = f"Bearer {settings.UNITY_ADMIN_KEY}"
    else:
        incoming_auth_header = request.headers.get("Authorization")
        if incoming_auth_header:
            auth_header = incoming_auth_header
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin key is not configured.",
        )

    service_url = (
        f"https://unity-{assistant_id}-262420637606.us-central1.run.app/status"
    )
    headers = {"Authorization": auth_header}

    try:
        response = requests.get(service_url, headers=headers, timeout=10)
        response.raise_for_status()
        return InfoResponse(info=response.json())
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Assistant service for ID '{assistant_id}' not found or failed to respond.",
            )
        try:
            detail = e.response.json().get("detail", str(e))
        except requests.exceptions.JSONDecodeError:
            detail = str(e)
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Assistant service returned an error: {detail}",
        )
    except requests.exceptions.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Connection to assistant service failed: {str(e)}",
        )
