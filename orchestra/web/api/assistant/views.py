import asyncio
import base64
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
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dao.voice_dao import VoiceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    DemoAssistantMeta,
    LogEvent,
    LogEventContext,
    Organization,
    OrganizationMember,
    Project,
)
from orchestra.lib.billing import get_billing_entity
from orchestra.services.bucket_service import BucketService
from orchestra.services.cartesia_service import CartesiaAPIError, CartesiaService
from orchestra.services.deepgram_service import DeepgramAPIError, DeepgramService
from orchestra.services.elevenlabs_service import ElevenLabsAPIError, ElevenLabsService
from orchestra.services.openai_service import OpenAIAPIError, OpenAIService
from orchestra.services.replicate_service import ReplicateAPIError, ReplicateService
from orchestra.settings import settings
from orchestra.web.api.assistant.schema import (
    AdminUpdateAssistant,
    AdminUpdateAssistantResponse,
    AdminUpdateUserByAssistant,
    AdminUpdateUserByAssistantResponse,
    AssistantContactRemoval,
    AssistantCreate,
    AssistantPhotoUploadResponse,
    AssistantRead,
    AssistantSpendingLimitResponse,
    AssistantSpendResponse,
    AssistantStatus,
    AssistantTransferResponse,
    AssistantTransferToOrgRequest,
    AssistantTransferToPersonalRequest,
    AssistantUpdate,
    AssistantVideoUploadResponse,
    Contact,
    DemoAssistantCreate,
    DemoAssistantMetaRead,
    InfoResponse,
    PhotoGenerateRequest,
    ReplicatePredictionResponse,
    SecretCreate,
    SecretRead,
    SecretUpdate,
    SpendingLimitRequest,
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
    create_vm,
    delete_email,
    delete_phone_number,
    delete_pubsub_topic,
    delete_vm,
    get_running_jobs,
    get_social_platforms_costs,
    log_pre_hire_chat,
    reawaken_assistant,
    stop_jobs,
    wake_up_assistant,
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
demo_router = APIRouter()


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
                            "phone_country": "US",
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
async def create_assistant(
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
    user_dao = UserDAO(session)
    assistant_dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    log_event_dao = LogEventDAO(session, context_dao)
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
            platforms_response = await get_social_platforms_costs()
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
        # Get organization context from API key (None = personal, int = org)
        organization_id = getattr(request.state, "organization_id", None)
        resource_access_dao = ResourceAccessDAO(session)
        role_dao = RoleDAO(session)

        # For org context, check assistant:write permission
        if organization_id is not None:
            has_permission = resource_access_dao.check_org_member_permission(
                user_id,
                organization_id,
                "assistant:write",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to create assistants in this organization.",
                )

        if not settings.is_staging:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            if billing_entity.credits < total_creation_cost:
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
            nationality=assistant_in.nationality,
            profile_photo=assistant_in.profile_photo,
            profile_video=assistant_in.profile_video,
            desktop_url=assistant_in.desktop_url,
            desktop_mode=assistant_in.desktop_mode,
            user_desktop_mode=assistant_in.user_desktop_mode,
            user_desktop_filesys_sync=assistant_in.user_desktop_filesys_sync or False,
            user_desktop_url=assistant_in.user_desktop_url,
            about=assistant_in.about,
            weekly_limit=parsed_weekly_limit,
            max_parallel=assistant_in.max_parallel,
            voice_id=assistant_in.voice_id,
            voice_provider=assistant_in.voice_provider,
            voice_mode=assistant_in.voice_mode,
            phone=None,
            email=assistant_in.email,
            phone_country=assistant_in.phone_country,
            user_whatsapp_number=assistant_in.user_whatsapp_number,
            timezone=assistant_in.timezone,
            organization_id=organization_id,
        )

        # For org assistants, grant Owner role to creator
        if organization_id is not None:
            owner_role = role_dao.get_by_name("Owner", organization_id=None)
            if owner_role:
                resource_access_dao.grant_access(
                    resource_type="assistant",
                    resource_id=assistant.agent_id,
                    role_id=owner_role.id,
                    grantee_type="user",
                    grantee_id=user_id,
                )

        # Create "Assistants" project if it doesn't exist (for logging purposes)
        ASSISTANTS_PROJECT_NAME = "Assistants"

        if organization_id is not None:
            # For org context, check if project exists in org (not user-access based)
            org_projects = project_dao.filter(
                organization_id=organization_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            assistants_project = org_projects[0][0] if org_projects else None

            if not assistants_project:
                # Create org Assistants project
                project_dao.create(
                    user_id=None,
                    organization_id=organization_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all organization assistants.",
                    is_versioned=False,
                )
                session.flush()

                # Fetch the created project
                org_projects = project_dao.filter(
                    organization_id=organization_id,
                    name=ASSISTANTS_PROJECT_NAME,
                )
                assistants_project = org_projects[0][0] if org_projects else None

                # Grant Owner role to creator
                if assistants_project:
                    owner_role = role_dao.get_by_name("Owner", organization_id=None)
                    if owner_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=assistants_project.id,
                            role_id=owner_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )

                    # Grant Member access to all other existing org members
                    org_members = organization_member_dao.filter(
                        organization_id=organization_id,
                    )
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        for member_row in org_members:
                            member = member_row[0]
                            if member.user_id != user_id:
                                resource_access_dao.grant_access(
                                    resource_type="project",
                                    resource_id=assistants_project.id,
                                    role_id=member_role.id,
                                    grantee_type="user",
                                    grantee_id=member.user_id,
                                )
            else:
                # Project exists - check if user already has access
                has_access = resource_access_dao.check_user_permission(
                    user_id,
                    "project",
                    assistants_project.id,
                    "project:read",
                )
                if not has_access:
                    # Grant Member role to user
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=assistants_project.id,
                            role_id=member_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )
        else:
            # Personal API key - check user access
            assistants_project = project_dao.get_by_user_and_name(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
                organization_id=None,
            )
            if not assistants_project:
                # Create personal Assistants project
                project_dao.create(
                    user_id=user_id,
                    organization_id=None,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all your assistants.",
                    is_versioned=False,
                )

        # Commit the assistant creation before infrastructure setup
        # This ensures the assistant persists even if we refresh the session later
        session.commit()

        assistant_id = assistant.agent_id
        # Infrastructure creation with rollback on failure
        created_email = None
        created_phone = None
        created_pubsub = None
        assigned_whatsapp = None
        created_vm = None

        if assistant_in.create_infra:
            current_infra_step = "initializing"
            try:
                # Step 1 & 2: create and watch email
                if assistant_in.email:
                    email_local = (
                        assistant_in.email.split("@")[0]
                        if "@" in assistant_in.email
                        else assistant_in.email
                    )
                    current_infra_step = "create_email"
                    email_response = await create_email(
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

                    await asyncio.sleep(10)
                    current_infra_step = "watch_email"
                    watch_response = await watch_email(
                        created_email,
                        is_staging=settings.is_staging,
                    )
                    print(watch_response)
                    if "detail" in watch_response:
                        raise Exception(
                            f"Email watch setup failed: {watch_response['detail']}",
                        )
                    print(f"EMAIL WATCHED: {created_email}")

                # Step 3: create phone number if user_phone is provided
                if assistant_in.user_phone:
                    phone_country = (
                        assistant_in.phone_country
                        if assistant_in.phone_country
                        else "US"
                    )
                    current_infra_step = "create_phone_number"
                    phone_response = await create_phone_number(
                        phone_country=phone_country,
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
                    current_infra_step = "assign_whatsapp_sender"
                    assigned_whatsapp = (
                        await assign_whatsapp_sender(
                            assistant_in.user_whatsapp_number,
                            is_staging=settings.is_staging,
                        )
                    )["whatsapp_number"]

                # Step 5: create pubsub topic
                current_infra_step = "create_pubsub_topic"
                pubsub_response = await create_pubsub_topic(
                    str(assistant_id),
                    is_staging=settings.is_staging,
                )
                if "detail" in pubsub_response:
                    raise Exception(
                        f"Pubsub topic creation failed: {pubsub_response['detail']}",
                    )
                created_pubsub = True
                print(f"PUBSUB CREATED: {assistant_id}")

                # Step 6: Create VM if desktop_mode is windows/ubuntu
                if assistant_in.desktop_mode in ("windows", "ubuntu"):
                    current_infra_step = "create_vm"
                    vm_response = await create_vm(
                        assistant_id=str(assistant_id),
                        unify_apikey=request.state.api_key,
                        assistant_name=str(assistant_id),
                        vm_type=assistant_in.desktop_mode,
                    )
                    if "detail" in vm_response or "error" in vm_response:
                        raise Exception(
                            f"VM creation failed: {vm_response.get('detail') or vm_response.get('error')}",
                        )
                    created_vm = vm_response
                    print(f"VM CREATED ({assistant_in.desktop_mode}): {assistant_id}")

                # Refresh database session after long infrastructure operations
                logging.info(
                    f"Refreshing database session after infrastructure setup for assistant {assistant_id}",
                )
                session.close()
                session = next(get_db_session(request))
                assistant_dao = AssistantDAO(session)

                # Update assistant with created infrastructure details
                update_data = {
                    "email": created_email,
                    "phone": created_phone,
                    "user_phone": assistant_in.user_phone,
                    "user_whatsapp_number": assistant_in.user_whatsapp_number,
                    "assistant_whatsapp_number": assigned_whatsapp,
                }
                # Add desktop_url from VM creation if applicable
                if created_vm and created_vm.get("desktop_url"):
                    update_data["desktop_url"] = created_vm["desktop_url"]
                assistant_dao.update_assistant(
                    user_id=user_id,
                    agent_id=assistant_id,
                    update_data=update_data,
                )
                # Commit the infrastructure updates
                session.commit()
                print(f"ASSISTANT UPDATED: {assistant_id}")

                # Retrieve the updated assistant for the final response
                assistant = assistant_dao.get_assistant_by_id(
                    user_id=user_id,
                    agent_id=assistant_id,
                    organization_id=organization_id,
                )

            except Exception as infra_error:
                # Use repr() to always show exception type, even if str() is empty
                print(
                    f"INFRA ERROR at step '{current_infra_step}': "
                    f"{type(infra_error).__name__}: {infra_error!r}",
                )

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

                # Delete VM first (created last)
                if created_vm:
                    try:
                        await delete_vm(
                            str(assistant_id),
                            vm_type=assistant_in.desktop_mode,
                        )
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete VM: {str(e)}")
                    print(f"VM DELETED ({assistant_in.desktop_mode}): {assistant_id}")

                if created_pubsub:
                    try:
                        await delete_pubsub_topic(
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
                        await delete_phone_number(created_phone)
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete phone: {str(e)}")
                print(f"PHONE DELETED: {created_phone}")

                if created_email:
                    try:
                        await delete_email(created_email)
                    except Exception as e:
                        rollback_errors.append(f"Failed to delete email: {str(e)}")
                print(f"EMAIL DELETED: {created_email}")

                # Delete the assistant record since infrastructure failed
                try:
                    # First, delete the chat context if it was created
                    if assistant_in.pre_hire_chat:
                        try:
                            context_name = f"{user_id}/{assistant_id}/Transcripts"
                            assistants_project = project_dao.get_by_user_and_name(
                                user_id=user_id,
                                name="Assistants",
                                organization_id=None,
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

    # Phase 2: Deduct credits from the correct billing account (user or org).
    if not settings.is_staging:
        try:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(session, billing_entity, Decimal(str(total_creation_cost)))
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

    # Phase 3: Wake up assistant
    response = await wake_up_assistant(
        assistant.agent_id,
        is_staging=settings.is_staging,
    )
    if response.status_code != 200:
        logging.error(f"Failed to wake up assistant: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to wake up assistant.",
        )
    else:
        print(f"ASSISTANT AWAKENED: {assistant.agent_id}")

    # (Optional) Log pre-hire chat if provided
    if assistant_in.pre_hire_chat:
        try:
            # Convert Pydantic models to dictionaries for the webhook payload
            chat_messages = jsonable_encoder(assistant_in.pre_hire_chat)
            await log_pre_hire_chat(
                assistant_id=str(assistant.agent_id),
                messages=chat_messages,
                is_staging=settings.is_staging,
            )
        except Exception as e_log:
            # We don't rollback the whole assistant creation for a logging failure,
            # but we should log it as a warning.
            logging.warning(
                f"Failed to log pre-hire chat for assistant {assistant.agent_id} via webhook. Error: {str(e_log)}",
            )

    # Phase 4: Prepare and return response
    return InfoResponse(
        info=AssistantRead(
            agent_id=str(assistant.agent_id),
            user_id=assistant.user_id,
            organization_id=assistant.organization_id,
            first_name=assistant.first_name,
            surname=assistant.surname,
            age=assistant.age,
            nationality=assistant.nationality,
            profile_photo=assistant.profile_photo,
            profile_video=assistant.profile_video,
            desktop_url=assistant.desktop_url,
            desktop_mode=assistant.desktop_mode,
            user_desktop_mode=assistant.user_desktop_mode,
            user_desktop_filesys_sync=assistant.user_desktop_filesys_sync,
            user_desktop_url=assistant.user_desktop_url,
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
            voice_provider=assistant.voice_provider,
            voice_mode=assistant.voice_mode,
            phone_country=assistant.phone_country,
            user_whatsapp_number=assistant.user_whatsapp_number,
            assistant_whatsapp_number=assistant.assistant_whatsapp_number,
            user_phone=assistant.user_phone,
            timezone=assistant.timezone,
            demo_id=assistant.demo_id,
            monthly_spending_cap=(
                float(assistant.monthly_spending_cap)
                if assistant.monthly_spending_cap is not None
                else None
            ),
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
                                "nationality": "United States",
                                "profile_photo": "https://example.com/photos/alice.jpg",
                                "profile_video": "https://example.com/videos/alice.mp4",
                                "about": "Mathematician and writer known for work on Analytical Engine",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "voice_provider": "cartesia",
                                "voice_mode": "tts",
                                "phone_country": "US",
                                "timezone": "America/New_York",
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
                                "nationality": "Mexico",
                                "profile_photo": "https://example.com/photos/bob.jpg",
                                "profile_video": "https://example.com/videos/bob.mp4",
                                "about": "Machine learning expert with focus on computer vision",
                                "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                                "voice_provider": "cartesia",
                                "voice_mode": "tts",
                                "phone_country": "CA",
                                "timezone": "America/Vancouver",
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
    list_all_org: bool = Query(
        False,
        description="If True and using an org API key, list ALL assistants in the organization (not just those created by the current user). Requires assistant:read permission.",
    ),
    demo: bool = Query(
        False,
        description="If True, include demo assistants in results.",
    ),
    demo_only: bool = Query(
        False,
        description="If True, only return demo assistants.",
    ),
) -> InfoResponse[List[AssistantRead]]:
    """
    List assistants based on API key context.

    For personal API key: Returns all personal assistants created by the user.
    For org API key (list_all_org=False): Returns assistants created by the user in this org.
    For org API key (list_all_org=True): Returns ALL assistants in the org (requires assistant:read permission).
    """
    # Correct for URL-decoded '+' in query parameters.
    phone = normalize_phone_parameter(phone)

    assistant_dao = AssistantDAO(session)
    user_id = request.state.user_id

    # Get organization context from API key
    organization_id = getattr(request.state, "organization_id", None)

    try:
        if organization_id is not None and list_all_org:
            # Org context with list_all_org=True: list all org assistants
            # Check if user has assistant:read permission
            resource_access_dao = ResourceAccessDAO(session)
            has_permission = resource_access_dao.check_org_member_permission(
                user_id,
                organization_id,
                "assistant:read",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to view all assistants in this organization.",
                )
            assistants = assistant_dao.list_all_org_assistants(
                organization_id=organization_id,
                phone=phone,
                email=email,
                include_demo=demo,
                demo_only=demo_only,
            )
        else:
            # Personal context OR org context with list_all_org=False
            assistants = assistant_dao.list_assistants_for_user(
                user_id,
                organization_id=organization_id,
                phone=phone,
                email=email,
                include_demo=demo,
                demo_only=demo_only,
            )
        voice_dao = VoiceDAO(session)

        return InfoResponse(
            info=[
                AssistantRead(
                    agent_id=str(a.agent_id),
                    user_id=a.user_id,
                    organization_id=a.organization_id,
                    first_name=a.first_name,
                    surname=a.surname,
                    age=a.age,
                    nationality=a.nationality,
                    profile_photo=a.profile_photo,
                    profile_video=a.profile_video,
                    desktop_url=a.desktop_url,
                    desktop_mode=a.desktop_mode,
                    user_desktop_mode=a.user_desktop_mode,
                    user_desktop_filesys_sync=a.user_desktop_filesys_sync,
                    user_desktop_url=a.user_desktop_url,
                    about=a.about,
                    phone_country=a.phone_country,
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
                    voice_id=a.voice_id,
                    voice_provider=a.voice_provider,
                    voice_mode=a.voice_mode,
                    timezone=a.timezone,
                    demo_id=a.demo_id,
                    monthly_spending_cap=(
                        float(a.monthly_spending_cap)
                        if a.monthly_spending_cap is not None
                        else None
                    ),
                )
                for a in assistants
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error fetching assistants: {str(e)}",
        )


@router.delete(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Remove a contact method from an assistant",
    description="Removes a contact method (phone, email, or WhatsApp) from an assistant and deprovisions the associated infrastructure.",
    tags=["Assistant Management"],
    responses={
        200: {
            "description": "Contact method removed successfully.",
        },
        404: {
            "description": "Assistant not found.",
        },
        400: {
            "description": "Invalid contact type or other error.",
        },
    },
)
async def delete_assistant_contact(
    assistant_id: int,
    removal_payload: AssistantContactRemoval,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Remove a contact method from an assistant.

    This endpoint deprovisions the infrastructure for a specific contact method
    (e.g., deletes the Twilio phone number) and removes the information from the
    assistant's record.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )

    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # For org assistants, check assistant:write permission
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
            )

    contact_type = removal_payload.contact_type

    try:
        if contact_type == "phone":
            if assistant.phone:
                await delete_phone_number(assistant.phone)
            assistant.phone = None
            assistant.user_phone = None
        elif contact_type == "email":
            if assistant.email:
                await delete_email(assistant.email)
            assistant.email = None
        elif contact_type == "whatsapp":
            # No external infra deletion for WhatsApp based on existing delete_assistant logic
            assistant.user_whatsapp_number = None
            assistant.assistant_whatsapp_number = None

        session.commit()
        session.refresh(assistant)
        updated_assistant = assistant

        # After successfully updating, trigger a reawaken
        try:
            await reawaken_assistant(
                str(updated_assistant.agent_id),
                is_staging=settings.is_staging,
            )
        except Exception as e:
            # Log the error but don't fail the request, as the main action succeeded
            logging.warning(
                f"Failed to reawaken assistant {updated_assistant.agent_id} after contact deletion: {e}",
            )

        return InfoResponse(
            info=AssistantRead(
                agent_id=str(updated_assistant.agent_id),
                user_id=updated_assistant.user_id,
                organization_id=updated_assistant.organization_id,
                first_name=updated_assistant.first_name,
                surname=updated_assistant.surname,
                age=updated_assistant.age,
                nationality=updated_assistant.nationality,
                profile_photo=updated_assistant.profile_photo,
                profile_video=updated_assistant.profile_video,
                desktop_url=updated_assistant.desktop_url,
                desktop_mode=updated_assistant.desktop_mode,
                user_desktop_mode=updated_assistant.user_desktop_mode,
                user_desktop_filesys_sync=updated_assistant.user_desktop_filesys_sync,
                user_desktop_url=updated_assistant.user_desktop_url,
                about=updated_assistant.about,
                phone_country=updated_assistant.phone_country,
                weekly_limit=(
                    float(updated_assistant.weekly_limit)
                    if updated_assistant.weekly_limit is not None
                    else None
                ),
                max_parallel=updated_assistant.max_parallel,
                created_at=updated_assistant.created_at,
                updated_at=updated_assistant.updated_at,
                phone=updated_assistant.phone,
                user_phone=updated_assistant.user_phone,
                user_whatsapp_number=updated_assistant.user_whatsapp_number,
                assistant_whatsapp_number=updated_assistant.assistant_whatsapp_number,
                email=updated_assistant.email,
                voice_id=updated_assistant.voice_id,
                voice_provider=updated_assistant.voice_provider,
                voice_mode=updated_assistant.voice_mode,
                timezone=updated_assistant.timezone,
                demo_id=updated_assistant.demo_id,
                monthly_spending_cap=(
                    float(updated_assistant.monthly_spending_cap)
                    if updated_assistant.monthly_spending_cap is not None
                    else None
                ),
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Failed to delete contact for assistant {assistant_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove contact: {str(e)}",
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
async def delete_assistant(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    """
    Delete an assistant by ID for the authenticated user.

    Permanently removes the specified assistant from the user's account or organization.
    This action cannot be undone. Associated GCS profile photos will also be deleted.
    """
    bucket_service = BucketService()
    dao = AssistantDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    organization_id = getattr(request.state, "organization_id", None)
    cleanup_errors = []
    try:
        # First get the assistant to retrieve infrastructure details including GCS photo URL
        assistant = dao.get_assistant_by_id(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            logging.warning(
                f"Assistant with ID {assistant_id} not found for user {request.state.user_id}.",
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant not found.",
            )

        # For org assistants, check assistant:delete permission
        if organization_id is not None:
            resource_access_dao = ResourceAccessDAO(session)
            has_permission = resource_access_dao.check_user_permission(
                request.state.user_id,
                "assistant",
                assistant_id,
                "assistant:delete",
            )
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You do not have permission to delete this assistant.",
                )

        # Suspend any jobs that might be currently running with that assistant
        try:
            response = await stop_jobs(assistant_id, session)
            print(f"JOB STOPPED: {response['job_names']}")
        except Exception as e:
            logging.error(f"Failed to stop job: {str(e)}")
            cleanup_errors.append(f"Failed to stop job: {str(e)}")

        # Delete VM if assistant has one (desktop_mode is windows/ubuntu)
        if assistant.desktop_mode in ("windows", "ubuntu"):
            try:
                vm_response = await delete_vm(
                    str(assistant_id),
                    vm_type=assistant.desktop_mode,
                )
                if not vm_response.get("vm_deleted"):
                    cleanup_errors.append(
                        f"VM deletion reported issues: {vm_response}",
                    )
            except Exception as e:
                logging.error(f"Failed to delete VM: {str(e)}")
                cleanup_errors.append(f"Failed to delete VM: {str(e)}")
            print(f"VM DELETED ({assistant.desktop_mode}): {assistant_id}")

        # Delete the associated chat transcript context from the "Assistants" project
        try:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            if organization_id is not None:
                # Org context: lookup directly by org_id + name (no user access check needed)
                assistants_project = (
                    session.query(Project)
                    .filter(
                        Project.organization_id == organization_id,
                        Project.name == ASSISTANTS_PROJECT_NAME,
                    )
                    .first()
                )
            else:
                # Personal context: use user access check
                assistants_project = project_dao.get_by_user_and_name(
                    user_id=request.state.user_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    organization_id=None,
                )
            if assistants_project:
                assistant_context_id = str(assistant_id)
                user_ctx = request.state.user_id
                context_prefix = f"{user_ctx}/{assistant_context_id}"
                contexts_to_delete = (
                    session.query(Context)
                    .filter(
                        Context.project_id == assistants_project.id,
                        or_(
                            Context.name == context_prefix,
                            Context.name.like(f"{context_prefix}/%"),
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

        # Delete all assistant GCS data (recordings, media, attachments) under {assistant_id}/
        try:
            cleanup_counts = bucket_service.delete_all_assistant_data(
                assistant_id,
                is_staging=settings.is_staging,
            )
            total = sum(cleanup_counts.values())
            if total > 0:
                print(
                    f"GCS CLEANUP: {total} file(s) deleted "
                    f"(media={cleanup_counts['media']}, "
                    f"recordings={cleanup_counts['recordings']}, "
                    f"attachments={cleanup_counts['attachments']})",
                )
        except Exception as e:
            logging.error(
                f"Failed to clean up GCS data for assistant {assistant_id}: {str(e)}",
            )
            cleanup_errors.append(f"Failed to clean up GCS data: {str(e)}")

        # Wait before starting other infra cleanup (same as rollback operations)
        await asyncio.sleep(10)

        # Delete pubsub topic
        try:
            await delete_pubsub_topic(str(assistant_id), is_staging=settings.is_staging)
        except Exception as e:
            cleanup_errors.append(f"Failed to delete pubsub topic: {str(e)}")
        print(f"PUBSUB DELETED: {assistant_id}")

        # Delete phone number if exists
        if assistant.phone:
            try:
                await delete_phone_number(assistant.phone)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete phone: {str(e)}")
        print(f"PHONE DELETED: {assistant.phone}")

        # Delete email if exists (with debug print like rollback)
        if assistant.email:
            try:
                await delete_email(assistant.email)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete email: {str(e)}")
        print(f"EMAIL DELETED: {assistant.email}")

        # Delete demo assistant metadata if this is a demo assistant
        if assistant.demo_id:
            try:
                demo_meta = (
                    session.query(DemoAssistantMeta)
                    .filter(
                        DemoAssistantMeta.id == assistant.demo_id,
                    )
                    .first()
                )
                if demo_meta:
                    session.delete(demo_meta)
            except Exception as e:
                cleanup_errors.append(f"Failed to delete demo metadata: {str(e)}")

        # Finally delete the assistant record (matching rollback error handling)
        try:
            dao.delete_assistant(
                user_id=request.state.user_id,
                agent_id=assistant_id,
                organization_id=organization_id,
            )
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
                            "nationality": "United States",
                            "profile_photo": "https://example.com/photos/alice.jpg",
                            "profile_video": "https://example.com/videos/alice.mp4",
                            "voice_id": "bf0a246a-8642-498a-9950-80c35e9276b5",
                            "voice_provider": "cartesia",
                            "voice_mode": "tts",
                            "phone_country": "US",
                            "timezone": "America/New_York",
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
async def update_assistant_config(
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
    organization_id = getattr(request.state, "organization_id", None)
    user_dao = UserDAO(session)
    assistant_dao = AssistantDAO(session)
    bucket_service = BucketService()

    # Store the old photo URL before the update
    old_photo_url = None
    is_photo_changing = False
    old_video_url = None
    is_video_changing = False

    # Variables to track newly created resources for potential rollback
    email_to_update: Optional[str] = None
    phone_to_update: Optional[str] = None
    contact_info_updated = (
        update.phone or update.user_phone or update.email or update.user_whatsapp_number
    )

    # Check assistant existence before any updates
    existing_assistant = assistant_dao.get_assistant_by_id(
        user_id=request.state.user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not existing_assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    # For org assistants, check assistant:write permission
    if organization_id is not None:
        resource_access_dao = ResourceAccessDAO(session)
        has_permission = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:write",
        )
        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this assistant.",
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

    # Initialize variables with values from the update payload or existing record
    assistant_email = update.email
    assistant_phone = update.phone
    assistant_whatsapp_number = (
        existing_assistant.assistant_whatsapp_number
        if existing_assistant.assistant_whatsapp_number
        else None
    )

    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        if update.create_infra:
            # Create / update assistant email
            # 1- Check if the assistant doesn't have an email address already and if an assistant email is provided
            # 2- If so, create an assistant email
            if update.email and not existing_assistant.email:
                try:
                    email_local = (
                        update.email.split("@")[0]
                        if "@" in update.email
                        else update.email
                    )
                    email_response = await create_email(
                        email_local,
                        existing_assistant.first_name,
                        existing_assistant.surname,
                    )
                    if "detail" in email_response:
                        raise Exception(
                            f"Email creation failed on assistant update: {email_response['detail']}",
                        )
                    email_to_update = email_response.get("user").get("primaryEmail")
                    print(f"EMAIL CREATED ON ASSISTANT UPDATE: {email_to_update}")

                    await asyncio.sleep(10)
                    watch_response = await watch_email(
                        email_to_update,
                        is_staging=settings.is_staging,
                    )
                    print(watch_response)
                    if "detail" in watch_response:
                        raise Exception(
                            f"Email watch setup failed: {watch_response['detail']}",
                        )
                    print(f"EMAIL WATCHED ON ASSISTANT UPDATE: {email_to_update}")

                    assistant_email = email_to_update

                except Exception as e:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to create email during update: {str(e)}",
                    )

            # Create / update assistant phone
            # 1- Check if the assistant doesn't have a phone number already and if a user phone is provided
            # 2- If so, create an assistant phone number
            if update.user_phone and not existing_assistant.phone:
                try:
                    phone_country = (
                        update.phone_country if update.phone_country else "US"
                    )
                    phone_response = await create_phone_number(
                        phone_country=phone_country,
                        is_staging=settings.is_staging,
                    )
                    if "detail" in phone_response:
                        raise Exception(
                            f"Phone number creation failed: {phone_response['detail']}",
                        )
                    phone_to_update = phone_response.get("phoneNumber")
                    assistant_phone = phone_to_update
                    print(f"PHONE CREATED ON UPDATE: {phone_to_update}")
                except Exception as e:
                    # If phone creation fails, we should not proceed with the update
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to create phone number during update: {str(e)}",
                    )

            # Create / update social account:
            # 1- Check if the assistant doesn't have a user account already and if a user account value is provided
            # 2- If so and if user has enough credits (production), assign the whatsapp account to the assistant
            if (
                update.user_whatsapp_number
                and not existing_assistant.user_whatsapp_number
            ):
                if not settings.is_staging:
                    # Cost to create a social account
                    try:
                        platforms_response = await get_social_platforms_costs()
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
                    decimal_cost = Decimal(cost)
                    try:
                        billing_entity = get_billing_entity(
                            session,
                            user_id,
                            organization_id,
                        )
                    except ValueError:
                        raise HTTPException(
                            status_code=status.HTTP_402_PAYMENT_REQUIRED,
                            detail="Billing is not set up. Please add a payment method first.",
                        )
                    if billing_entity.credits < decimal_cost:
                        raise HTTPException(
                            status_code=status.HTTP_402_PAYMENT_REQUIRED,
                            detail="Insufficient credits to add a WhatsApp number.",
                        )
                    from orchestra.lib.billing import deduct_credits

                    deduct_credits(session, billing_entity, decimal_cost)

                assistant_whatsapp_number = (
                    await assign_whatsapp_sender(
                        update.user_whatsapp_number,
                        is_staging=settings.is_staging,
                    )
                )["whatsapp_number"]

        update_data = update.model_dump(exclude_unset=True)
        if "create_infra" in update_data:
            del update_data["create_infra"]
        if "weekly_limit" in update_data and update.weekly_limit is not None:
            update_data["weekly_limit"] = Decimal(update.weekly_limit)
        if (
            "monthly_spending_cap" in update_data
            and update.monthly_spending_cap is not None
        ):
            update_data["monthly_spending_cap"] = Decimal(
                str(update.monthly_spending_cap),
            )
        if assistant_email:
            update_data["email"] = assistant_email
        if assistant_phone:
            update_data["phone"] = assistant_phone
        if assistant_whatsapp_number:
            update_data["assistant_whatsapp_number"] = assistant_whatsapp_number

        updated = assistant_dao.update_assistant(
            user_id=request.state.user_id,
            agent_id=assistant_id,
            update_data=update_data,
            organization_id=organization_id,
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

        session.commit()

        # If contact info was updated and infra creation was enabled, reawaken the assistant
        if contact_info_updated and update.create_infra:
            try:
                await reawaken_assistant(
                    str(updated.agent_id),
                    is_staging=settings.is_staging,
                )
                print(f"ASSISTANT REAWAKENED: {updated.agent_id}")
            except Exception as e:
                # Log the error but don't fail the request, as the main action succeeded
                logging.warning(
                    f"Failed to reawaken assistant {updated.agent_id} after config update: {e}",
                )

        return InfoResponse(
            info=AssistantRead(
                agent_id=str(updated.agent_id),
                user_id=updated.user_id,
                organization_id=updated.organization_id,
                first_name=updated.first_name,
                surname=updated.surname,
                age=updated.age,
                nationality=updated.nationality,
                profile_photo=updated.profile_photo,
                profile_video=updated.profile_video,
                desktop_url=updated.desktop_url,
                desktop_mode=updated.desktop_mode,
                user_desktop_mode=updated.user_desktop_mode,
                user_desktop_filesys_sync=updated.user_desktop_filesys_sync,
                user_desktop_url=updated.user_desktop_url,
                about=updated.about,
                phone_country=updated.phone_country,
                weekly_limit=(
                    float(updated.weekly_limit)
                    if updated.weekly_limit is not None
                    else None
                ),
                max_parallel=updated.max_parallel,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
                phone=assistant_phone,
                email=assistant_email,
                user_whatsapp_number=updated.user_whatsapp_number,
                assistant_whatsapp_number=assistant_whatsapp_number,
                user_phone=updated.user_phone,
                voice_id=updated.voice_id,
                voice_provider=updated.voice_provider,
                voice_mode=updated.voice_mode,
                timezone=updated.timezone,
                demo_id=updated.demo_id,
                monthly_spending_cap=(
                    float(updated.monthly_spending_cap)
                    if updated.monthly_spending_cap is not None
                    else None
                ),
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()

        if email_to_update:
            logging.warning(
                f"Update failed. Rolling back created email: {email_to_update}",
            )
            try:
                await delete_email(email_to_update)
            except Exception as cleanup_err:
                logging.error(
                    f"Failed to clean up (delete) email '{email_to_update}' during rollback: {cleanup_err}",
                )

        if phone_to_update:
            logging.warning(
                f"Update failed. Rolling back created phone number: {phone_to_update}",
            )
            try:
                await delete_phone_number(phone_to_update)
            except Exception as cleanup_err:
                logging.error(
                    f"Failed to clean up (delete) phone number '{phone_to_update}' during rollback: {cleanup_err}",
                )

        if isinstance(e, HTTPException):
            raise e

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating assistant config: {str(e)}",
        )


@router.post(
    "/assistant/{assistant_id}/transfer/to-org",
    response_model=InfoResponse[AssistantTransferResponse],
    status_code=status.HTTP_200_OK,
    summary="Transfer assistant to organization",
    description="Transfers a personal assistant to an organizational workspace.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Assistant transferred successfully"},
        403: {"description": "Permission denied"},
        404: {"description": "Assistant not found"},
        400: {"description": "Invalid transfer request"},
    },
)
def transfer_assistant_to_org(
    assistant_id: int,
    transfer_request: AssistantTransferToOrgRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantTransferResponse]:
    """
    Transfer a personal assistant to an organization.

    This endpoint:
    1. Moves the assistant from personal workspace to organizational workspace
    2. Optionally transfers logs from personal "Assistants" project to org "Assistants" project
    3. Grants the transferring user Owner role on the assistant in the org
    4. Updates the assistant's associated API key to the org API key
    """
    user_id = request.state.user_id
    target_org_id = transfer_request.organization_id
    assistant_dao = AssistantDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)

    # Verify this is a personal assistant (must use personal API key)
    current_org_id = getattr(request.state, "organization_id", None)
    if current_org_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must use a personal API key to transfer personal assistants. Use an org API key for org assistants.",
        )

    # Get the personal assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=None,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal assistant not found.",
        )

    # Check user has assistant:write permission in target org
    has_permission = resource_access_dao.check_org_member_permission(
        user_id,
        target_org_id,
        "assistant:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create assistants in the target organization.",
        )

    logs_transferred = False
    try:
        # Transfer logs if requested
        if transfer_request.transfer_logs:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            # Get personal Assistants project
            personal_project = project_dao.get_by_user_and_name(
                user_id=user_id,
                name=ASSISTANTS_PROJECT_NAME,
                organization_id=None,
            )
            # Get or create org Assistants project
            # Use filter() instead of get_by_user_and_name() because we need to find
            # org projects directly without requiring user access checks
            org_projects = project_dao.filter(
                organization_id=target_org_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            org_project = org_projects[0][0] if org_projects else None
            project_created = False
            if not org_project:
                project_dao.create(
                    user_id=None,
                    organization_id=target_org_id,
                    name=ASSISTANTS_PROJECT_NAME,
                    description="Project to manage and track all organization assistants.",
                    is_versioned=False,
                )
                session.flush()  # Get project ID
                org_projects = project_dao.filter(
                    organization_id=target_org_id,
                    name=ASSISTANTS_PROJECT_NAME,
                )
                org_project = org_projects[0][0] if org_projects else None
                project_created = True

            # Grant access to the Assistants project for the transferring user
            if org_project:
                owner_role = role_dao.get_by_name("Owner", organization_id=None)
                if project_created:
                    # Creator gets Owner role
                    if owner_role:
                        resource_access_dao.grant_access(
                            resource_type="project",
                            resource_id=org_project.id,
                            role_id=owner_role.id,
                            grantee_type="user",
                            grantee_id=user_id,
                        )

                    # Grant Member access to all other existing org members
                    org_members = organization_member_dao.filter(
                        organization_id=target_org_id,
                    )
                    member_role = role_dao.get_by_name("Member", organization_id=None)
                    if member_role:
                        for member_row in org_members:
                            member = member_row[0]
                            if member.user_id != user_id:
                                resource_access_dao.grant_access(
                                    resource_type="project",
                                    resource_id=org_project.id,
                                    role_id=member_role.id,
                                    grantee_type="user",
                                    grantee_id=member.user_id,
                                )
                else:
                    # Project exists - check if user already has access
                    has_access = resource_access_dao.check_user_permission(
                        user_id,
                        "project",
                        org_project.id,
                        "project:read",
                    )
                    if not has_access:
                        # Grant Member role to user
                        member_role = role_dao.get_by_name(
                            "Member",
                            organization_id=None,
                        )
                        if member_role:
                            resource_access_dao.grant_access(
                                resource_type="project",
                                resource_id=org_project.id,
                                role_id=member_role.id,
                                grantee_type="user",
                                grantee_id=user_id,
                            )

            if personal_project and org_project:
                assistant_context_id = str(assistant_id)
                context_prefix = f"{user_id}/{assistant_context_id}"
                contexts_to_transfer = (
                    session.query(Context)
                    .filter(
                        Context.project_id == personal_project.id,
                        or_(
                            Context.name == context_prefix,
                            Context.name.like(f"{context_prefix}/%"),
                        ),
                    )
                    .all()
                )
                for ctx in contexts_to_transfer:
                    # Update LogEvent.project_id for all log events in this context
                    # This is required because logs are queried by LogEvent.project_id
                    session.query(LogEvent).filter(
                        LogEvent.id.in_(
                            session.query(LogEventContext.log_event_id).filter(
                                LogEventContext.context_id == ctx.id,
                            ),
                        ),
                    ).update(
                        {LogEvent.project_id: org_project.id},
                        synchronize_session=False,
                    )
                    # Update the context's project_id
                    ctx.project_id = org_project.id

                # =========================================================
                # Transfer logs from shared aggregate contexts (3-tier hierarchy)
                # - Tier 1: All/* (global aggregate)
                # - Tier 2: User/All/* (user aggregate)
                # These contexts may contain logs from multiple assistants,
                # so we only transfer logs where _assistant_id matches
                # =========================================================
                shared_contexts = (
                    session.query(Context)
                    .filter(
                        Context.project_id == personal_project.id,
                        or_(
                            Context.name.like("All/%"),  # Tier 1: All/*
                            Context.name.like("%/All/%"),  # Tier 2: User/All/*
                        ),
                    )
                    .all()
                )

                shared_logs_transferred = False
                for shared_ctx in shared_contexts:
                    # Find logs belonging to this assistant in the shared context
                    assistant_log_ids = [
                        row[0]
                        for row in (
                            session.query(LogEventContext.log_event_id)
                            .join(
                                LogEvent,
                                LogEvent.id == LogEventContext.log_event_id,
                            )
                            .filter(
                                LogEventContext.context_id == shared_ctx.id,
                                LogEvent.data["_assistant_id"].astext
                                == str(assistant_id),
                            )
                            .all()
                        )
                    ]

                    if not assistant_log_ids:
                        continue

                    shared_logs_transferred = True

                    # Check if shared context exists in org project
                    org_shared_ctx = (
                        session.query(Context)
                        .filter(
                            Context.project_id == org_project.id,
                            Context.name == shared_ctx.name,
                        )
                        .first()
                    )

                    if org_shared_ctx:
                        target_ctx_id = org_shared_ctx.id
                    else:
                        # Create the shared context in org project
                        new_ctx = Context(
                            project_id=org_project.id,
                            name=shared_ctx.name,
                        )
                        session.add(new_ctx)
                        session.flush()
                        target_ctx_id = new_ctx.id

                    # Move logs to org project
                    session.query(LogEvent).filter(
                        LogEvent.id.in_(assistant_log_ids),
                    ).update(
                        {LogEvent.project_id: org_project.id},
                        synchronize_session=False,
                    )

                    # Update context links to point to org's context
                    session.query(LogEventContext).filter(
                        LogEventContext.log_event_id.in_(assistant_log_ids),
                        LogEventContext.context_id == shared_ctx.id,
                    ).update(
                        {LogEventContext.context_id: target_ctx_id},
                        synchronize_session=False,
                    )

                logs_transferred = (
                    len(contexts_to_transfer) > 0 or shared_logs_transferred
                )

        # Transfer the assistant to org
        transferred = assistant_dao.transfer_to_organization(
            agent_id=assistant_id,
            user_id=user_id,
            organization_id=target_org_id,
        )
        if not transferred:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to transfer assistant.",
            )

        # Grant Owner role to the user on this assistant
        owner_role = role_dao.get_by_name("Owner", organization_id=None)
        if owner_role:
            resource_access_dao.grant_access(
                resource_type="assistant",
                resource_id=assistant_id,
                role_id=owner_role.id,
                grantee_type="user",
                grantee_id=user_id,
            )

        session.commit()

        return InfoResponse(
            info=AssistantTransferResponse(
                message="Assistant transferred to organization successfully.",
                agent_id=assistant_id,
                transferred_from="personal",
                transferred_to="organization",
                logs_transferred=logs_transferred,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Failed to transfer assistant {assistant_id} to org: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transfer assistant: {str(e)}",
        )


@router.post(
    "/assistant/{assistant_id}/transfer/to-personal",
    response_model=InfoResponse[AssistantTransferResponse],
    status_code=status.HTTP_200_OK,
    summary="Transfer assistant to personal workspace",
    description="Transfers an organizational assistant to the user's personal workspace.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Assistant transferred successfully"},
        403: {"description": "Permission denied"},
        404: {"description": "Assistant not found"},
        400: {"description": "Invalid transfer request"},
    },
)
def transfer_assistant_to_personal(
    assistant_id: int,
    transfer_request: AssistantTransferToPersonalRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantTransferResponse]:
    """
    Transfer an organizational assistant to personal workspace.

    This endpoint:
    1. Moves the assistant from org workspace to user's personal workspace
    2. Deletes related logs from org "Assistants" project if requested
    3. Removes RBAC grants on the assistant
    4. Updates the assistant's owner to the requesting user
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    resource_access_dao = ResourceAccessDAO(session)

    # Verify this is an org assistant (must use org API key)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must use an organization API key to transfer org assistants.",
        )

    # Get the org assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization assistant not found.",
        )

    # Check user has assistant:delete permission on this assistant
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "assistant",
        assistant_id,
        "assistant:delete",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to transfer this assistant out of the organization.",
        )

    logs_deleted = False
    try:
        # Delete logs if requested
        if transfer_request.delete_logs:
            ASSISTANTS_PROJECT_NAME = "Assistants"
            # Use filter() instead of get_by_user_and_name() because we need to find
            # org projects directly without requiring user access checks
            org_projects = project_dao.filter(
                organization_id=organization_id,
                name=ASSISTANTS_PROJECT_NAME,
            )
            org_project = org_projects[0][0] if org_projects else None
            if org_project:
                assistant_context_id = str(assistant_id)
                contexts_to_delete = (
                    session.query(Context)
                    .filter(
                        Context.project_id == org_project.id,
                        or_(
                            Context.name == assistant_context_id,
                            Context.name.like(f"{assistant_context_id}/%"),
                            Context.name.like(f"%/{assistant_context_id}"),
                            Context.name.like(f"%/{assistant_context_id}/%"),
                        ),
                    )
                    .all()
                )
                for ctx in contexts_to_delete:
                    context_dao.delete(ctx.id)

                logs_deleted = len(contexts_to_delete) > 0

        # Remove all RBAC grants on this assistant
        existing_grants = resource_access_dao.get_resource_access(
            resource_type="assistant",
            resource_id=assistant_id,
        )
        for grant in existing_grants:
            resource_access_dao.revoke_access(
                resource_type="assistant",
                resource_id=assistant_id,
                grantee_type=grant.grantee_type,
                grantee_id=grant.grantee_id,
            )

        # Transfer the assistant to personal
        transferred = assistant_dao.transfer_to_personal(
            agent_id=assistant_id,
            organization_id=organization_id,
            new_owner_user_id=user_id,
        )
        if not transferred:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to transfer assistant.",
            )

        session.commit()

        return InfoResponse(
            info=AssistantTransferResponse(
                message="Assistant transferred to personal workspace successfully.",
                agent_id=assistant_id,
                transferred_from="organization",
                transferred_to="personal",
                logs_deleted=logs_deleted,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Failed to transfer assistant {assistant_id} to personal: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transfer assistant: {str(e)}",
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
                provider_service.delete_voice(new_voice_id)
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
async def delete_voice(
    voice_id: str,
    request: Request,
    provider: str = Query(..., description="The provider of the voice to delete"),
    session: Session = Depends(get_db_session),
    cartesia_service: CartesiaService = Depends(),
    elevenlabs_service: ElevenLabsService = Depends(),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    voice_dao = VoiceDAO(session)

    # First, get the voice to check its existence and preset status.
    voice_to_delete = voice_dao.get_voice_by_id(
        user_id=user_id,
        voice_id=voice_id,
        provider=provider,
    )
    if not voice_to_delete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice not found for this user.",
        )

    try:
        # Attempt to delete from our DB first. The DAO method contains the
        # "in-use" validation and will raise a 409 Conflict if necessary.
        voice_dao.delete_voice(user_id=user_id, voice_id=voice_id, provider=provider)

        # If the voice is not a preset, also delete it from the provider.
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
                    # If the provider says "not found," it's a non-critical error.
                    # We can proceed since our goal is to have it deleted.
                    if e_provider.status_code == 404:
                        logging.warning(
                            f"Voice {voice_id} not found on {voice_to_delete.provider} during deletion attempt. Continuing with DB deletion.",
                        )
                    else:
                        # For other provider errors, we must roll back our DB change.
                        raise e_provider  # This will be caught below.

        # If both DB and provider deletions were successful (or skippable), commit.
        session.commit()
        return InfoResponse(info="Voice deleted successfully.")

    except HTTPException as e:
        session.rollback()
        raise e
    except (CartesiaAPIError, ElevenLabsAPIError) as e_provider:
        session.rollback()
        logging.error(
            f"Critical provider error deleting voice {voice_id} from {provider}: {e_provider.detail}",
        )
        raise HTTPException(
            status_code=e_provider.status_code,
            detail=f"Failed to delete voice from {provider}: {e_provider.detail}",
        )
    except Exception as e_generic:
        session.rollback()
        logging.error(
            f"Generic error during voice deletion {voice_id}: {str(e_generic)}",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting voice from database: {str(e_generic)}",
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
    openai_service: OpenAIService = Depends(),
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
        elif request_data.provider == "openai":
            audio_bytes, content_type = openai_service.generate_speech(
                text=request_data.text,
                voice_id=request_data.voice_id,
                model_id=request_data.model_id or "gpt-4o-mini-tts",
                output_format=request_data.output_format,
            )
        else:
            # This case should be prevented by Pydantic's Literal validation
            raise HTTPException(
                status_code=400,
                detail="Invalid TTS provider specified.",
            )

        return Response(content=audio_bytes, media_type=content_type)

    except (CartesiaAPIError, ElevenLabsAPIError, OpenAIAPIError) as e:
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
    "/assistant/{assistant_id}/secret",
    response_model=InfoResponse[SecretRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create or update a secret",
    description=(
        "Creates a new secret for an assistant or updates an existing one. "
        "Secrets are used to store API keys and tokens for external services."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Assistant not found"},
        status.HTTP_409_CONFLICT: {
            "description": "Secret already exists (use PUT to update)",
        },
    },
    tags=["Secrets"],
)
def create_secret(
    assistant_id: int,
    secret_in: SecretCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[SecretRead]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    secret_dao = AssistantSecretDAO(session)

    # Verify access to the assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id,
        assistant_id,
        organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    try:
        secret = secret_dao.create_secret(
            user_id=user_id,
            agent_id=assistant_id,
            secret_name=secret_in.secret_name,
            secret_value=secret_in.secret_value,
            description=secret_in.description,
        )
        session.commit()
        return InfoResponse(
            info=SecretRead(
                secret_name=secret.secret_name,
                description=secret.description,
                created_at=secret.created_at,
                updated_at=secret.updated_at,
            ),
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Secret '{secret_in.secret_name}' already exists for this assistant. Use PUT to update.",
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error creating secret: {str(e)}",
        )


@router.put(
    "/assistant/{assistant_id}/secret/{secret_name}",
    response_model=InfoResponse[SecretRead],
    status_code=status.HTTP_200_OK,
    summary="Update secret",
    description="Updates an existing secret's value and/or description.",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Assistant or secret not found"},
    },
    tags=["Secrets"],
)
def update_secret(
    assistant_id: int,
    secret_name: str,
    secret_in: SecretUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[SecretRead]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    secret_dao = AssistantSecretDAO(session)

    # Verify access to the assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id,
        assistant_id,
        organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    secret = secret_dao.update_secret(
        user_id=user_id,
        agent_id=assistant_id,
        secret_name=secret_name,
        secret_value=secret_in.secret_value,
        description=secret_in.description,
    )

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Secret '{secret_name}' not found.",
        )

    session.commit()
    return InfoResponse(
        info=SecretRead(
            secret_name=secret.secret_name,
            description=secret.description,
            created_at=secret.created_at,
            updated_at=secret.updated_at,
        ),
    )


@router.delete(
    "/assistant/{assistant_id}/secret/{secret_name}",
    status_code=status.HTTP_200_OK,
    summary="Delete secret",
    description="Deletes a specific secret from an assistant.",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Assistant or secret not found"},
    },
    tags=["Secrets"],
)
def delete_secret(
    assistant_id: int,
    secret_name: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[dict]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)
    secret_dao = AssistantSecretDAO(session)

    # Verify access to the assistant
    assistant = assistant_dao.get_assistant_by_id(
        user_id,
        assistant_id,
        organization_id,
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )

    try:
        secret_dao.delete_secret(user_id, assistant_id, secret_name)
        session.commit()
        return InfoResponse(
            info={"message": f"Secret '{secret_name}' deleted successfully."},
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error deleting secret: {str(e)}",
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
async def generate_assistant_photo(
    request: Request,
    payload: PhotoGenerateRequest,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    openai_service: OpenAIService = Depends(),
) -> InfoResponse[str]:
    """
    Generate a new assistant profile photo from a text prompt.

    This endpoint uses an AI model to generate an image based on the provided
    text prompt. The user's account is charged for this operation.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

    # 1. Moderate the prompt
    try:
        moderation_result = openai_service.moderate_text(payload.prompt)
        if moderation_result.is_nsfw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Prompt moderation failed: {moderation_result.reason}",
            )
    except OpenAIAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Content moderation check failed: {e.detail}",
        )

    # 2. Pre-check credits if not in staging
    if not settings.is_staging:
        try:
            billing_entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Billing is not set up. Please add a payment method first.",
            )
        if billing_entity.credits < settings.photo_generation_cost:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Insufficient credits to generate a photo.",
            )

    # 3. Generate photo
    try:
        image_url = replicate_service.generate_photo(
            prompt=payload.prompt,
            aspect_ratio=payload.aspect_ratio,
            output_format=payload.output_format,
            output_quality=payload.output_quality,
            safety_tolerance=payload.safety_tolerance,
            prompt_upsampling=payload.prompt_upsampling,
        )

        # 4. Deduct credits after successful generation if not in staging
        if not settings.is_staging:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(
                session,
                billing_entity,
                Decimal(str(settings.photo_generation_cost)),
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
    openai_service: OpenAIService = Depends(),
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
    organization_id = getattr(request.state, "organization_id", None)

    temp_gcs_url_to_delete: Optional[str] = None
    input_image_for_replicate: Optional[str] = None

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

        # 1. Moderate inputs
        try:
            # Moderate text prompt
            prompt_moderation = openai_service.moderate_text(prompt)
            if prompt_moderation.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Prompt moderation failed: {prompt_moderation.reason}",
                )

            # Moderate input image
            image_analysis = openai_service.analyze_image(
                image_url=input_image_for_replicate,
            )
            if image_analysis.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Image moderation failed: {image_analysis.reason}",
                )
        except OpenAIAPIError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Content moderation check failed: {e.detail}",
            )
        except HTTPException:
            raise

        # 2. Pre-check credits if not in staging
        if not settings.is_staging:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            if billing_entity.credits < settings.photo_generation_cost:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to edit a photo.",
                )

        # 3. Edit Photo
        image_url = replicate_service.edit_photo(
            prompt=prompt,
            input_image=input_image_for_replicate,
            aspect_ratio=aspect_ratio,
            output_format=output_format,
            safety_tolerance=safety_tolerance,
        )

        # 4. Deduct credits after successful edit if not in staging
        if not settings.is_staging:
            from orchestra.lib.billing import deduct_credits

            edit_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(
                session,
                edit_entity,
                Decimal(str(settings.photo_generation_cost)),
            )
            session.commit()

        return InfoResponse(info=image_url)

    except ReplicateAPIError as e:
        session.rollback()
        logging.error(f"Replicate API error: {e.detail}")
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )
    except HTTPException as http_e:
        session.rollback()
        logging.error(f"Could not edit photo: {str(http_e)}")
        raise
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
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Animate photo",
    description="Starts a job to generate an animated video of the assistant using an input image and audio. This action costs credits.",
    tags=["Media"],
)
async def animate_video_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
    replicate_service: ReplicateService = Depends(),
    bucket_service: BucketService = Depends(),
    openai_service: OpenAIService = Depends(),
    image_url: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    audio_url: Optional[str] = Form(None),
    audio_file: Optional[UploadFile] = File(None),
    seed: Optional[int] = Form(None),
    duration: Optional[int] = Form(None),
) -> InfoResponse[ReplicatePredictionResponse]:
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)

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
            public_img_url, gcs_img_url = bucket_service.upload_temp_assistant_file(
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

        try:
            # Perform content moderation and analysis
            image_analysis = openai_service.analyze_image(
                image_url=final_image_url_for_replicate,
            )
            if not image_analysis.has_human_face:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Animation requires an image with a clear human face. Reason: {image_analysis.reason}",
                )
            if image_analysis.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Image moderation failed: The image was flagged as inappropriate. Reason: {image_analysis.reason}",
                )

            audio_analysis = openai_service.analyze_audio(
                audio_url=final_audio_url_for_replicate,
            )
            # New check for speech content
            if not audio_analysis.contains_speech:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Audio moderation failed: No speech was detected in the audio file. Reason: {audio_analysis.reason}",
                )
            if audio_analysis.is_nsfw:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Audio moderation failed: The audio was flagged as inappropriate. Reason: {audio_analysis.reason}",
                )

        except OpenAIAPIError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Content moderation check failed: {e.detail}",
            )
        except HTTPException:
            raise
        except Exception as e:
            logging.error(
                f"An unexpected error occurred during content moderation for user {user_id}: {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred during content moderation.",
            )

        # Pre-check credits (assuming video_generation_cost is defined in settings)
        if not settings.is_staging:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            video_cost = settings.video_generation_cost * (
                duration if duration is not None else settings.default_video_duration
            )
            if billing_entity.credits < video_cost:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to generate video.",
                )

        animation_kwargs = {
            "image_url": final_image_url_for_replicate,
            "audio_url": final_audio_url_for_replicate,
            "seed": seed,
        }
        if duration is not None:
            animation_kwargs["duration"] = duration
        prediction = replicate_service.create_video_animation(**animation_kwargs)

        # Deduct credits after successful generation
        if not settings.is_staging:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(
                session,
                billing_entity,
                Decimal(str(settings.video_generation_cost)),
            )
            session.commit()

        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)

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


@router.get(
    "/assistant/photo/animate/{prediction_id}",
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_200_OK,
    summary="Get animation prediction status",
    description="Retrieves the status and result of a video animation job.",
    tags=["Media"],
)
def get_animation_prediction(
    prediction_id: str,
    request: Request,
    replicate_service: ReplicateService = Depends(),
):
    try:
        prediction = replicate_service.get_prediction(prediction_id)
        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)
    except ReplicateAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )


@router.post(
    "/assistant/photo/animate/{prediction_id}/cancel",
    response_model=InfoResponse[ReplicatePredictionResponse],
    status_code=status.HTTP_200_OK,
    summary="Cancel animation prediction",
    description="Cancels a running video animation job.",
    tags=["Media"],
)
def cancel_animation_prediction(
    prediction_id: str,
    request: Request,
    replicate_service: ReplicateService = Depends(),
):
    try:
        prediction = replicate_service.cancel_prediction(prediction_id)
        response_data = ReplicatePredictionResponse.from_orm(prediction)
        return InfoResponse(info=response_data)
    except ReplicateAPIError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Replicate API error: {e.detail}",
        )


##################
# Admin endpoints #
##################


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
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantStatus]:
    """
    Get the live status of an assistant's dedicated service.
    """
    try:
        job_names = get_running_jobs(assistant_id, session)
        if len(job_names) > 0:
            return InfoResponse(
                info=AssistantStatus(running=True, job_name=job_names[0]),
            )
        else:
            return InfoResponse(info=AssistantStatus(running=False, job_name=None))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get assistant status: {str(e)}",
        )


@admin_router.post(
    "/assistant/update-user",
    response_model=AdminUpdateUserByAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin: Update user details via assistant lookup",
    description="Updates a user's profile (timezone, bio) by looking up the assistant. "
    "For personal assistants, updates the owner. "
    "For org assistants, finds the member by email and updates them.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "User updated successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "User updated successfully",
                        "user_id": "abc123",
                        "email": "user@example.com",
                        "assistant_type": "personal",
                    },
                },
            },
        },
        404: {
            "description": "Assistant or user not found.",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
        422: {
            "description": "Validation error (e.g., invalid timezone).",
        },
    },
)
def admin_update_user_by_assistant(
    request_body: AdminUpdateUserByAssistant,
    session: Session = Depends(get_db_session),
) -> AdminUpdateUserByAssistantResponse:
    """
    Update a user's profile by looking up an assistant.

    For personal assistants: updates the owner's profile if email matches.
    For org assistants: finds the org member by email and updates their profile.
    """
    assistant_dao = AssistantDAO(session)
    user_dao = UserDAO(session)
    org_member_dao = OrganizationMemberDAO(session)

    # Get assistant without user/org context (admin bypass)
    assistant = assistant_dao.get_assistant_by_agent_id(request_body.assistant_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant with id {request_body.assistant_id} not found.",
        )

    target_user_id = None
    assistant_type = "personal"

    if assistant.organization_id is None:
        # Personal assistant: check if target_user_email matches owner
        assistant_type = "personal"
        owner = user_dao.get_by_id(assistant.user_id)
        if not owner:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Assistant owner not found.",
            )
        # owner is a tuple (User,)
        owner_user = owner[0]
        if owner_user.email != request_body.target_user_email:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target user '{request_body.target_user_email}' does not match "
                f"assistant owner.",
            )
        target_user_id = owner_user.id
    else:
        # Org assistant: find member by email
        assistant_type = "organization"
        members = org_member_dao.filter(organization_id=assistant.organization_id)

        # Find member whose email matches target_user_email
        for member_tuple in members:
            member = member_tuple[0]
            user_row = user_dao.get_by_id(member.user_id)
            if user_row:
                user = user_row[0]
                if user.email == request_body.target_user_email:
                    target_user_id = user.id
                    break

        if target_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target user '{request_body.target_user_email}' not found "
                f"in organization.",
            )

    # Build update kwargs (only include non-None values)
    update_kwargs = {}
    if request_body.timezone is not None:
        update_kwargs["timezone"] = request_body.timezone
    if request_body.bio is not None:
        update_kwargs["bio"] = request_body.bio

    if not update_kwargs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update. Provide at least 'timezone' or 'bio'.",
        )

    # Update the user
    try:
        user_dao.update(id=target_user_id, **update_kwargs)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    return AdminUpdateUserByAssistantResponse(
        info="User updated successfully",
        user_id=target_user_id,
        email=request_body.target_user_email,
        assistant_type=assistant_type,
    )


@admin_router.patch(
    "/assistant/{assistant_id}",
    response_model=AdminUpdateAssistantResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin: Update assistant details",
    description="Updates an assistant's details (timezone, about) directly, "
    "bypassing permission checks.",
    tags=["Assistants", "Admin"],
    responses={
        200: {
            "description": "Assistant updated successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Assistant updated successfully",
                        "assistant_id": 123,
                        "updated_fields": ["timezone", "about"],
                    },
                },
            },
        },
        404: {
            "description": "Assistant not found.",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
        422: {
            "description": "Validation error (e.g., invalid timezone).",
        },
    },
)
def admin_update_assistant(
    assistant_id: int,
    request_body: AdminUpdateAssistant,
    session: Session = Depends(get_db_session),
) -> AdminUpdateAssistantResponse:
    """
    Update an assistant's details directly (admin bypass).

    Updates timezone and/or about fields without requiring user context.
    """
    assistant_dao = AssistantDAO(session)

    # Get assistant without user/org context (admin bypass)
    assistant = assistant_dao.get_assistant_by_agent_id(assistant_id)
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant with id {assistant_id} not found.",
        )

    # Build update dict and track updated fields
    updated_fields = []

    if request_body.timezone is not None:
        assistant.timezone = request_body.timezone
        updated_fields.append("timezone")

    if request_body.about is not None:
        assistant.about = request_body.about
        updated_fields.append("about")

    if not updated_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update. Provide at least 'timezone' or 'about'.",
        )

    # Commit changes
    session.commit()

    return AdminUpdateAssistantResponse(
        info="Assistant updated successfully",
        assistant_id=assistant_id,
        updated_fields=updated_fields,
    )


@admin_router.get(
    "/assistant",
    summary="Admin: list all assistants",
    description="Retrieve every assistant in the system, optionally filtered by phone or email. "
    "Use 'fields' parameter for selective field retrieval to improve performance.",
    tags=["Assistants", "Admin"],
)
def admin_list_all_assistants(
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this E.164-style value (leading '+' is URL-encoded).",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Only return assistants whose user phone number matches this value.",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose user WhatsApp number matches this value.",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose assistant WhatsApp number matches this value.",
    ),
    agent_id: Optional[int] = Query(
        None,
        description="Only return assistants whose agent_id matches this value.",
    ),
    from_fields: Optional[str] = Query(
        None,
        description="Comma-separated list of fields to return (e.g., 'email,agent_id,phone'). "
        "If omitted, returns full AssistantRead objects. Using this parameter skips "
        "expensive lookups (api_key, secrets, user info) when those fields aren't requested.",
        example="email,agent_id,first_name",
    ),
    session: Session = Depends(get_db_session),
):
    """
    List all assistants in the system with optional filtering and field selection.

    When 'from_fields' is specified, returns only the requested fields, skipping expensive
    database lookups for unrequested fields like api_key, secrets, and user details.

    When 'from_fields' is omitted, returns full AssistantRead objects.
    """
    # Normalize filter parameters to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_phone = normalize_phone_parameter(user_phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    assistant_dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    user_dao = UserDAO(session)
    secret_dao = AssistantSecretDAO(session)

    # Dynamically get all valid field names from AssistantRead model
    VALID_FIELDS = set(AssistantRead.model_fields.keys())

    # Parse and validate requested fields before any database operations
    requested_fields: Optional[set] = None
    if from_fields is not None and from_fields.strip():
        raw_fields = [f.strip() for f in from_fields.split(",") if f.strip()]

        if not raw_fields:
            raise HTTPException(
                status_code=422,
                detail="The 'from_fields' parameter cannot be empty. Provide comma-separated field names.",
            )

        invalid_fields = [f for f in raw_fields if f not in VALID_FIELDS]
        if invalid_fields:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid field name(s): {', '.join(sorted(invalid_fields))}. "
                f"Valid fields are: {', '.join(sorted(VALID_FIELDS))}",
            )

        requested_fields = set(raw_fields)

    try:
        assistants = assistant_dao.list_all_assistants(
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
            agent_id=agent_id,
        )

        # Get API key based on assistant type (personal vs organizational)
        def get_api_key_for_assistant(assistant):
            if assistant.organization_id is None:
                keys = api_key_dao.get_personal_keys(assistant.user_id)
            else:
                keys = api_key_dao.filter(organization_id=assistant.organization_id)
            return keys[0][0].key if keys else None

        # Get secrets for each assistant
        def get_secrets_for_assistant(assistant):
            secrets = secret_dao.list_secrets(
                assistant.user_id,
                assistant.agent_id,
            )
            return {s.secret_name: s.secret_value for s in secrets}

        # Perform expensive lookups only if needed
        api_keys = (
            [get_api_key_for_assistant(a) for a in assistants]
            if (requested_fields is None or "api_key" in requested_fields)
            else None
        )
        secrets_list = (
            [get_secrets_for_assistant(a) for a in assistants]
            if (requested_fields is None or "secrets" in requested_fields)
            else None
        )
        users = (
            [user_dao.get_by_id(a.user_id)[0] for a in assistants]
            if (
                requested_fields is None
                or bool(
                    requested_fields
                    & {"user_email", "user_first_name", "user_last_name"},
                )
            )
            else None
        )

        # Build AssistantRead objects
        assistant_reads = [
            AssistantRead(
                agent_id=str(a.agent_id),
                user_id=a.user_id,
                organization_id=a.organization_id,
                first_name=a.first_name,
                surname=a.surname,
                age=a.age,
                nationality=a.nationality,
                profile_photo=a.profile_photo,
                profile_video=a.profile_video,
                desktop_url=a.desktop_url,
                desktop_mode=a.desktop_mode,
                user_desktop_mode=a.user_desktop_mode,
                user_desktop_filesys_sync=a.user_desktop_filesys_sync,
                user_desktop_url=a.user_desktop_url,
                about=a.about,
                weekly_limit=(
                    float(a.weekly_limit) if a.weekly_limit is not None else None
                ),
                max_parallel=a.max_parallel,
                created_at=a.created_at,
                updated_at=a.updated_at,
                phone=a.phone,
                user_phone=a.user_phone,
                email=a.email,
                user_whatsapp_number=a.user_whatsapp_number,
                assistant_whatsapp_number=a.assistant_whatsapp_number,
                voice_id=a.voice_id,
                voice_provider=a.voice_provider,
                voice_mode=a.voice_mode,
                timezone=a.timezone,
                phone_country=a.phone_country,
                monthly_spending_cap=(
                    float(a.monthly_spending_cap)
                    if a.monthly_spending_cap is not None
                    else None
                ),
                demo_id=a.demo_id,
                # Expensive fields - only populated if needed
                api_key=api_keys[i] if api_keys else None,
                user_first_name=users[i].name if users else None,
                user_last_name=users[i].last_name if users else None,
                user_email=users[i].email if users else None,
                secrets=secrets_list[i] if secrets_list else None,
            )
            for i, a in enumerate(assistants)
        ]

        # If from_fields were requested, filter using Pydantic's model_dump
        if requested_fields is not None:
            result = [ar.model_dump(include=requested_fields) for ar in assistant_reads]
            return InfoResponse(info=result)

        # No from_fields parameter - return full AssistantRead objects
        return InfoResponse(info=assistant_reads)

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching assistants: {str(e)}",
        )


@admin_router.patch(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    summary="Admin: update assistant by filter",
    description="Update a single assistant based on unique filter parameters.",
    tags=["Assistants", "Admin"],
)
def admin_update_assistant_by_filter(
    phone: Optional[str] = Query(
        None,
        description="Filter: assistant phone number",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Filter: user phone number",
    ),
    email: Optional[str] = Query(
        None,
        description="Filter: assistant email address",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Filter: user WhatsApp number",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Filter: assistant WhatsApp number",
    ),
    new_assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="New WhatsApp number for the assistant",
    ),
    new_user_whatsapp_number: Optional[str] = Query(
        None,
        description="New WhatsApp number for the user",
    ),
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update a single assistant based on filter parameters.
    """
    # Normalize filter parameters and the new WhatsApp number to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_phone = normalize_phone_parameter(user_phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    new_assistant_whatsapp_number = normalize_phone_parameter(
        new_assistant_whatsapp_number,
    )
    new_user_whatsapp_number = normalize_phone_parameter(
        new_user_whatsapp_number,
    )

    # Find the assistant to update
    dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    secret_dao = AssistantSecretDAO(session)
    assistants = dao.list_all_assistants(
        phone=phone,
        user_phone=user_phone,
        email=email,
        user_whatsapp_number=user_whatsapp_number,
        assistant_whatsapp_number=assistant_whatsapp_number,
    )
    if not assistants:
        raise HTTPException(status_code=404, detail="Assistant not found.")
    if len(assistants) > 1:
        raise HTTPException(
            status_code=400,
            detail="Multiple assistants found for filters.",
        )
    a = assistants[0]

    # Update only the assistant WhatsApp number
    update_data = {}
    if new_assistant_whatsapp_number:
        update_data["assistant_whatsapp_number"] = new_assistant_whatsapp_number
    if new_user_whatsapp_number:
        update_data["user_whatsapp_number"] = new_user_whatsapp_number
    updated = dao.update_assistant(
        user_id=a.user_id,
        agent_id=a.agent_id,
        update_data=update_data,
    )
    session.commit()

    # Get API key based on assistant type (personal vs organizational)
    if updated.organization_id is None:
        keys = api_key_dao.get_personal_keys(updated.user_id)
    else:
        keys = api_key_dao.filter(organization_id=updated.organization_id)
    api_key = keys[0][0].key if keys else None

    # Get secrets for the assistant
    secrets = secret_dao.list_secrets(updated.user_id, updated.agent_id)
    secrets_dict = {s.secret_name: s.secret_value for s in secrets}

    # Return updated assistant
    return InfoResponse(
        info=AssistantRead(
            agent_id=str(updated.agent_id),
            user_id=updated.user_id,
            organization_id=updated.organization_id,
            first_name=updated.first_name,
            surname=updated.surname,
            age=updated.age,
            nationality=updated.nationality,
            profile_photo=updated.profile_photo,
            profile_video=updated.profile_video,
            desktop_url=updated.desktop_url,
            desktop_mode=updated.desktop_mode,
            user_desktop_mode=updated.user_desktop_mode,
            user_desktop_filesys_sync=updated.user_desktop_filesys_sync,
            user_desktop_url=updated.user_desktop_url,
            about=updated.about,
            phone_country=updated.phone_country,
            weekly_limit=(
                float(updated.weekly_limit)
                if updated.weekly_limit is not None
                else None
            ),
            max_parallel=updated.max_parallel,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            phone=updated.phone,
            user_phone=updated.user_phone,
            email=updated.email,
            user_whatsapp_number=updated.user_whatsapp_number,
            assistant_whatsapp_number=updated.assistant_whatsapp_number,
            voice_id=updated.voice_id,
            voice_provider=updated.voice_provider,
            voice_mode=updated.voice_mode,
            timezone=updated.timezone,
            demo_id=updated.demo_id,
            monthly_spending_cap=(
                float(updated.monthly_spending_cap)
                if updated.monthly_spending_cap is not None
                else None
            ),
            api_key=api_key,
            secrets=secrets_dict,
        ),
    )


@admin_router.get(
    "/assistant/user/{user_id}",
    response_model=InfoResponse[List[AssistantRead]],
    summary="Admin: list all assistants for a user",
    description="Retrieve all assistants for the specified user_id, optionally filtered by phone, email, or WhatsApp numbers.",
    tags=["Assistants", "Admin"],
)
def admin_list_assistants_for_user(
    user_id: str,
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this value.",
    ),
    user_phone: Optional[str] = Query(
        None,
        description="Only return assistants whose user phone number matches this value.",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    user_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose user WhatsApp number matches this value.",
    ),
    assistant_whatsapp_number: Optional[str] = Query(
        None,
        description="Only return assistants whose assistant WhatsApp number matches this value.",
    ),
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[AssistantRead]]:
    """List all assistants belonging to a given user, with optional filtering."""
    # Normalize phone parameter to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)
    user_whatsapp_number = normalize_phone_parameter(user_whatsapp_number)
    assistant_whatsapp_number = normalize_phone_parameter(assistant_whatsapp_number)
    dao = AssistantDAO(session)
    api_key_dao = ApiKeyDAO(session)
    secret_dao = AssistantSecretDAO(session)
    try:
        assistants = dao.list_assistants_for_user(
            user_id=user_id,
            phone=phone,
            user_phone=user_phone,
            email=email,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
        )

        # Get API key based on assistant type (personal vs organizational)
        def get_api_key_for_assistant(assistant):
            if assistant.organization_id is None:
                keys = api_key_dao.get_personal_keys(assistant.user_id)
            else:
                keys = api_key_dao.filter(organization_id=assistant.organization_id)
            return keys[0][0].key if keys else None

        # Get secrets for each assistant
        def get_secrets_for_assistant(assistant):
            secrets = secret_dao.list_secrets(
                assistant.user_id,
                assistant.agent_id,
            )
            return {s.secret_name: s.secret_value for s in secrets}

        api_keys = [get_api_key_for_assistant(a) for a in assistants]
        secrets_list = [get_secrets_for_assistant(a) for a in assistants]

        return InfoResponse(
            info=[
                AssistantRead(
                    agent_id=str(a.agent_id),
                    user_id=a.user_id,
                    organization_id=a.organization_id,
                    first_name=a.first_name,
                    surname=a.surname,
                    age=a.age,
                    nationality=a.nationality,
                    profile_photo=a.profile_photo,
                    profile_video=a.profile_video,
                    desktop_url=a.desktop_url,
                    desktop_mode=a.desktop_mode,
                    user_desktop_mode=a.user_desktop_mode,
                    user_desktop_filesys_sync=a.user_desktop_filesys_sync,
                    user_desktop_url=a.user_desktop_url,
                    about=a.about,
                    weekly_limit=(
                        float(a.weekly_limit) if a.weekly_limit is not None else None
                    ),
                    max_parallel=a.max_parallel,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                    phone=a.phone,
                    user_phone=a.user_phone,
                    email=a.email,
                    user_whatsapp_number=a.user_whatsapp_number,
                    assistant_whatsapp_number=a.assistant_whatsapp_number,
                    voice_id=a.voice_id,
                    voice_provider=a.voice_provider,
                    voice_mode=a.voice_mode,
                    timezone=a.timezone,
                    demo_id=a.demo_id,
                    monthly_spending_cap=(
                        float(a.monthly_spending_cap)
                        if a.monthly_spending_cap is not None
                        else None
                    ),
                    api_key=api_keys[i],
                    secrets=secrets_list[i],
                )
                for i, a in enumerate(assistants)
            ],
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching assistants for user {user_id}: {str(e)}",
        )


@admin_router.get(
    "/contacts",
    response_model=List[Contact],
    summary="Admin: list all contacts",
    description="List all contact-context logs, optionally filtered by email, phone, or WhatsApp number",
    tags=["Assistants", "Admin"],
)
def admin_list_contacts(
    email_address: Optional[str] = Query(None, description="Filter by email_address"),
    phone_number: Optional[str] = Query(None, description="Filter by phone_number"),
    whatsapp_number: Optional[str] = Query(
        None,
        description="Filter by whatsapp_number",
    ),
    session: Session = Depends(get_db_session),
) -> List[Contact]:
    """
    Retrieve all contact logs stored in any context containing "Contacts" (case-sensitive).
    Supports optional filtering on email, phone, or WhatsApp number.
    """
    from typing import Any, Dict

    # Find all context IDs whose name contains 'Contacts' (case-sensitive)
    ctx_ids = (
        session.execute(select(Context.id).where(Context.name.like("%Contacts%")))
        .scalars()
        .all()
    )
    if not ctx_ids:
        return []

    # Build field filters
    filters = {}
    if email_address is not None:
        filters["email_address"] = email_address
    if phone_number is not None:
        filters["phone_number"] = normalize_phone_parameter(phone_number)
    if whatsapp_number is not None:
        filters["whatsapp_number"] = normalize_phone_parameter(whatsapp_number)

    # Retrieve matching log_event IDs
    log_event_dao = LogEventDAO(session)
    if filters:
        event_ids = log_event_dao.get_ids_by_filter(
            project_id=None,
            filters=filters,
            context_ids=ctx_ids,
        )
    else:
        event_ids = []
        for cid in ctx_ids:
            rows = log_event_dao.filter(context_id=cid)
            for r in rows:
                evt = r[0]
                event_ids.append(evt.id)
    if not event_ids:
        return []

    # Fetch log entries and assemble contacts per event
    grouped: Dict[int, Dict[str, Any]] = {}

    # Query LogEvent.data directly
    query = select(LogEvent.id, LogEvent.data).where(LogEvent.id.in_(event_ids))
    rows = session.execute(query).all()

    for event_id, data in rows:
        # data is already a dict from JSONB column
        grouped[event_id] = dict(data) if data else {}

    # Fetch user_id for each log_event via project
    rows = session.execute(
        select(LogEvent.id, Project.user_id)
        .join(Project, LogEvent.project_id == Project.id)
        .where(LogEvent.id.in_(event_ids)),
    )
    user_map = {evt: uid for evt, uid in rows}

    # Build final contact list with user_id
    results = []
    for eid, data in grouped.items():
        contact: Dict[str, Any] = {}
        custom: Dict[str, Any] = {}
        for k, v in data.items():
            if k in (
                "first_name",
                "surname",
                "email_address",
                "phone_number",
                "whatsapp_number",
                "description",
            ):
                contact[k] = v
            else:
                custom[k] = v
        contact["custom_fields"] = custom
        contact["user_id"] = user_map.get(eid)
        results.append(contact)
    return results


# ============================================================================
# Spending Limit Endpoints
# ============================================================================


@router.get(
    "/assistant/{agent_id}/spending-limit",
    response_model=AssistantSpendingLimitResponse,
    responses={
        200: {
            "description": "Spending limit retrieved successfully",
        },
        404: {
            "description": "Assistant not found",
        },
    },
)
async def get_assistant_spending_limit(
    request: Request,
    agent_id: int,
    session: Session = Depends(get_db_session),
):
    """
    Get the monthly spending limit for an assistant.

    Returns the assistant's limit and effective limit (considering parent limits).
    """
    user_id = request.state.user_id

    # Get the assistant and verify access
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")

    # Verify user owns the assistant
    if assistant.user_id != user_id:
        raise HTTPException(status_code=404, detail="Assistant not found.")

    # Get the limit
    monthly_cap = assistant_dao.get_spending_cap(agent_id)

    # Calculate effective limit based on context
    effective_limit = monthly_cap
    if assistant.organization_id is not None:
        # Org assistant - check member and org limits
        from orchestra.db.dao.organization_dao import OrganizationDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO

        org_dao = OrganizationDAO(session)
        org_member_dao = OrganizationMemberDAO(session)

        org = org_dao.get(assistant.organization_id)
        member = org_member_dao.get_member(user_id, assistant.organization_id)

        parent_limits = []
        if member and member.monthly_spending_cap is not None:
            parent_limits.append(float(member.monthly_spending_cap))
        if org and org.monthly_spending_cap is not None:
            parent_limits.append(float(org.monthly_spending_cap))

        if parent_limits:
            parent_limit = min(parent_limits)
            if effective_limit is None:
                effective_limit = parent_limit
            else:
                effective_limit = min(effective_limit, parent_limit)
    else:
        # Personal assistant - check user limit
        user_row = UserDAO(session).get_by_id(user_id)
        if user_row:
            user = user_row[0]
            if user.monthly_spending_cap is not None:
                parent_limit = float(user.monthly_spending_cap)
                if effective_limit is None:
                    effective_limit = parent_limit
                else:
                    effective_limit = min(effective_limit, parent_limit)

    return AssistantSpendingLimitResponse(
        agent_id=agent_id,
        monthly_spending_cap=monthly_cap,
        effective_limit=effective_limit,
    )


@router.put(
    "/assistant/{agent_id}/spending-limit",
    response_model=AssistantSpendingLimitResponse,
    responses={
        200: {
            "description": "Spending limit set successfully",
            "content": {
                "application/json": {
                    "example": {
                        "agent_id": 123,
                        "monthly_spending_cap": 100.00,
                        "effective_limit": 100.00,
                    },
                },
            },
        },
        400: {
            "description": "Invalid limit",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Assistant limit cannot exceed user limit ($50.00)",
                    },
                },
            },
        },
        404: {
            "description": "Assistant not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Assistant not found."},
                },
            },
        },
    },
)
async def set_assistant_spending_limit(
    request: Request,
    agent_id: int,
    body: SpendingLimitRequest,
    session: Session = Depends(get_db_session),
):
    """
    Set the monthly spending limit for an assistant.

    For personal assistants (no organization):
    - Limit cannot exceed the user's personal spending limit

    For organizational assistants:
    - Limit cannot exceed the member's org spending limit
    - Limit cannot exceed the organization's spending limit

    Setting to null removes the limit.
    """
    user_id = request.state.user_id
    assistant_dao = AssistantDAO(session)

    try:
        result = assistant_dao.set_spending_cap(
            agent_id=agent_id,
            user_id=user_id,
            monthly_spending_cap=body.monthly_spending_cap,
        )
        session.commit()

        return AssistantSpendingLimitResponse(
            agent_id=agent_id,
            monthly_spending_cap=result.monthly_spending_cap,
            effective_limit=result.effective_limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Admin Spend Endpoints (for UniLLM service calls)
# ============================================================================


@admin_router.get("/assistant/{agent_id}/spend")
def admin_get_assistant_spend(
    agent_id: int,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
):
    """
    Admin endpoint: Get an assistant's cumulative spend for a given month.

    This endpoint is for internal service calls (e.g., UniLLM) and does not
    require the caller to own the assistant.
    """
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")

    cumulative_spend = assistant_dao.get_cumulative_spend(agent_id, month)
    limit = assistant_dao.get_spending_cap(agent_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    # Include credit balance from the billing account (for credit guard checks).
    # The billing account comes from the org (if org assistant) or the user.
    credit_balance = None
    if assistant.organization_id is not None:
        from orchestra.db.models.orchestra_models import Organization

        org = (
            session.query(Organization)
            .filter(Organization.id == assistant.organization_id)
            .first()
        )
        if org and org.billing_account:
            credit_balance = float(org.billing_account.credits)
    else:
        from orchestra.db.models.orchestra_models import User

        user = session.query(User).filter(User.id == assistant.user_id).first()
        if user and user.billing_account:
            credit_balance = float(user.billing_account.credits)

    return AssistantSpendResponse(
        agent_id=agent_id,
        month=month,
        cumulative_spend=cumulative_spend,
        limit=limit,
        limit_set_at=assistant.monthly_spending_cap_set_at,
        percent_used=percent_used,
        credit_balance=credit_balance,
    )


# ============================================================================
# Demo Assistant Endpoints
# ============================================================================


@demo_router.post(
    "/assistant",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a demo assistant",
    description="Create a demo assistant by cloning from a source assistant. Only available to Unify organization members.",
    tags=["Demo Assistants"],
    include_in_schema=False,  # Hidden from public API docs
)
async def create_demo_assistant(
    request: Request,
    demo_create: DemoAssistantCreate,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Create a demo assistant for product demonstrations.

    This endpoint is only available to members of the Unify organization.
    It clones configuration from a source assistant and provisions phone
    infrastructure for demo calls.
    """
    user_id = request.state.user_id

    # Validate user is in Unify organization
    unify_org_name = settings.orchestra_organization_name

    # Get the Unify organization
    org_query = (
        session.query(Organization)
        .filter(
            Organization.name == unify_org_name,
        )
        .first()
    )

    if not org_query:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo assistant creation requires Unify organization membership.",
        )

    # Check if user is a member of the Unify organization
    member = (
        session.query(OrganizationMember)
        .filter(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == org_query.id,
        )
        .first()
    )

    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of the Unify organization to create demo assistants.",
        )

    # Get the source assistant
    assistant_dao = AssistantDAO(session)
    source_assistant = assistant_dao.get_assistant_by_agent_id(
        agent_id=demo_create.source_assistant_id,
    )

    if not source_assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source assistant {demo_create.source_assistant_id} not found or you don't have access to it.",
        )

    # Check name uniqueness for the new assistant
    existing = assistant_dao.get_assistant_by_name(
        user_id=user_id,
        first_name=demo_create.first_name,
        surname=demo_create.surname,
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An assistant with name '{demo_create.first_name} {demo_create.surname}' already exists.",
        )

    try:
        # Create the demo metadata (including optional prospect details)
        demo_meta = DemoAssistantMeta(
            source_assistant_id=source_assistant.agent_id,
            demoer_user_id=user_id,
            label=demo_create.label,
            # Optional prospect details for pre-populating boss contact
            prospect_first_name=demo_create.prospect_first_name,
            prospect_surname=demo_create.prospect_surname,
            prospect_email=demo_create.prospect_email,
            prospect_phone=demo_create.prospect_phone,
        )
        session.add(demo_meta)
        session.flush()  # Get the demo_meta.id

        # Create the demo assistant, cloning config from source
        demo_assistant = Assistant(
            user_id=user_id,
            organization_id=None,  # Personal assistant for the demoer
            first_name=demo_create.first_name,
            surname=demo_create.surname,
            # Clone from source
            age=source_assistant.age,
            nationality=source_assistant.nationality,
            about=source_assistant.about,
            profile_photo=source_assistant.profile_photo,
            profile_video=source_assistant.profile_video,
            voice_id=source_assistant.voice_id,
            voice_provider=source_assistant.voice_provider,
            voice_mode=source_assistant.voice_mode,
            # Demo-specific settings
            user_phone=demo_create.demoer_phone,
            timezone="UTC",  # Default timezone for demos
            monthly_spending_cap=Decimal(str(demo_create.monthly_spending_cap)),
            # Link to demo metadata
            demo_id=demo_meta.id,
        )
        session.add(demo_assistant)
        session.flush()  # Get the agent_id

        # Provision phone infrastructure
        # Use provided phone_country, fallback to source assistant's country, then default to US
        phone_country = demo_create.phone_country or "US"
        try:
            phone_response = await create_phone_number(
                phone_country=phone_country,
                is_staging=settings.is_staging,
            )
            if "detail" in phone_response:
                raise Exception(f"Phone creation failed: {phone_response['detail']}")
            demo_assistant.phone = phone_response.get("phoneNumber")
            demo_assistant.phone_country = phone_country
        except Exception as e:
            logging.error(f"Failed to provision phone for demo assistant: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to provision phone number: {str(e)}",
            )

        # Optionally provision email infrastructure
        if demo_create.provision_email:
            try:
                email_local = assistant_dao.generate_unique_email_local(
                    demo_create.first_name,
                    demo_create.surname,
                )
                email_response = await create_email(
                    email_local,
                    demo_create.first_name,
                    demo_create.surname,
                )
                if "detail" in email_response:
                    raise Exception(
                        f"Email creation failed: {email_response['detail']}",
                    )
                created_email = email_response.get("user", {}).get("primaryEmail")
                demo_assistant.email = created_email
                logging.info(f"Email provisioned for demo assistant: {created_email}")

                # Wait and set up email watch
                await asyncio.sleep(10)
                watch_response = await watch_email(
                    created_email,
                    is_staging=settings.is_staging,
                )
                if "detail" in watch_response:
                    logging.warning(
                        f"Email watch setup failed for demo assistant: {watch_response['detail']}",
                    )
            except Exception as e:
                logging.error(f"Failed to provision email for demo assistant: {e}")
                # Don't fail the whole creation - email is optional
                # The phone is already provisioned, so we continue

        # Create pubsub topic
        try:
            await create_pubsub_topic(
                str(demo_assistant.agent_id),
                is_staging=settings.is_staging,
            )
        except Exception as e:
            logging.warning(f"Failed to create pubsub topic for demo assistant: {e}")

        # Commit the transaction BEFORE waking up the assistant
        # This ensures the assistant is visible to Adapters when it queries Orchestra
        session.commit()

        # Wake up the assistant with demo mode
        # This must happen AFTER commit so Adapters can find the assistant in the database
        try:
            await wake_up_assistant(
                str(demo_assistant.agent_id),
                is_staging=settings.is_staging,
            )
        except Exception as e:
            logging.warning(f"Failed to wake up demo assistant: {e}")

        return InfoResponse(
            info=AssistantRead(
                agent_id=str(demo_assistant.agent_id),
                user_id=demo_assistant.user_id,
                organization_id=demo_assistant.organization_id,
                first_name=demo_assistant.first_name,
                surname=demo_assistant.surname,
                age=demo_assistant.age,
                nationality=demo_assistant.nationality,
                profile_photo=demo_assistant.profile_photo,
                profile_video=demo_assistant.profile_video,
                desktop_url=demo_assistant.desktop_url,
                desktop_mode=demo_assistant.desktop_mode,
                user_desktop_mode=demo_assistant.user_desktop_mode,
                user_desktop_filesys_sync=demo_assistant.user_desktop_filesys_sync,
                user_desktop_url=demo_assistant.user_desktop_url,
                about=demo_assistant.about,
                phone_country=demo_assistant.phone_country,
                weekly_limit=None,
                max_parallel=demo_assistant.max_parallel,
                created_at=demo_assistant.created_at,
                updated_at=demo_assistant.updated_at,
                phone=demo_assistant.phone,
                user_phone=demo_assistant.user_phone,
                user_whatsapp_number=demo_assistant.user_whatsapp_number,
                assistant_whatsapp_number=demo_assistant.assistant_whatsapp_number,
                email=demo_assistant.email,
                voice_id=demo_assistant.voice_id,
                voice_provider=demo_assistant.voice_provider,
                voice_mode=demo_assistant.voice_mode,
                timezone=demo_assistant.timezone,
                demo_id=demo_assistant.demo_id,
                monthly_spending_cap=(
                    float(demo_assistant.monthly_spending_cap)
                    if demo_assistant.monthly_spending_cap
                    else None
                ),
            ),
        )

    except IntegrityError as e:
        session.rollback()
        logging.error(f"Database integrity error creating demo assistant: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create demo assistant due to a constraint violation.",
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Unexpected error creating demo assistant: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create demo assistant: {str(e)}",
        )


@demo_router.get(
    "/assistant/{demo_id}/meta",
    response_model=InfoResponse[DemoAssistantMetaRead],
    status_code=status.HTTP_200_OK,
    summary="Get demo assistant metadata",
    description="Get metadata for a demo assistant.",
    tags=["Demo Assistants"],
    include_in_schema=False,  # Hidden from public API docs
)
async def get_demo_assistant_meta(
    request: Request,
    demo_id: int,
    session: Session = Depends(get_db_session),
) -> InfoResponse[DemoAssistantMetaRead]:
    """
    Get metadata for a demo assistant.

    The caller must own an assistant with this demo_id.
    """
    user_id = request.state.user_id

    # Verify the user owns an assistant with this demo_id
    assistant = (
        session.query(Assistant)
        .filter(
            Assistant.demo_id == demo_id,
            Assistant.user_id == user_id,
        )
        .first()
    )

    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo assistant not found or you don't have access to it.",
        )

    # Get the demo metadata
    demo_meta = (
        session.query(DemoAssistantMeta)
        .filter(
            DemoAssistantMeta.id == demo_id,
        )
        .first()
    )

    if not demo_meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo metadata not found.",
        )

    return InfoResponse(
        info=DemoAssistantMetaRead(
            id=demo_meta.id,
            source_assistant_id=demo_meta.source_assistant_id,
            demoer_user_id=demo_meta.demoer_user_id,
            label=demo_meta.label,
            created_at=demo_meta.created_at,
            prospect_first_name=demo_meta.prospect_first_name,
            prospect_surname=demo_meta.prospect_surname,
            prospect_email=demo_meta.prospect_email,
            prospect_phone=demo_meta.prospect_phone,
        ),
    )


@demo_router.get(
    "/assistant/meta/list",
    response_model=InfoResponse[List[DemoAssistantMetaRead]],
    status_code=status.HTTP_200_OK,
    summary="List all demo assistant metadata for current user",
    description="List all demo assistant metadata for the authenticated user.",
    tags=["Demo Assistants"],
    include_in_schema=False,  # Hidden from public API docs
)
async def list_demo_assistant_meta(
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[DemoAssistantMetaRead]]:
    """
    List all demo assistant metadata for the authenticated user.

    Returns metadata for all demo assistants owned by the current user,
    including labels and prospect details for UI display.
    """
    user_id = request.state.user_id

    # Get all demo meta entries for assistants owned by this user
    demo_metas = (
        session.query(DemoAssistantMeta)
        .join(
            Assistant,
            Assistant.demo_id == DemoAssistantMeta.id,
        )
        .filter(
            Assistant.user_id == user_id,
        )
        .order_by(DemoAssistantMeta.created_at.desc())
        .all()
    )

    return InfoResponse(
        info=[
            DemoAssistantMetaRead(
                id=meta.id,
                source_assistant_id=meta.source_assistant_id,
                demoer_user_id=meta.demoer_user_id,
                label=meta.label,
                created_at=meta.created_at,
                prospect_first_name=meta.prospect_first_name,
                prospect_surname=meta.prospect_surname,
                prospect_email=meta.prospect_email,
                prospect_phone=meta.prospect_phone,
            )
            for meta in demo_metas
        ],
    )
