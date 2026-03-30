import asyncio
import base64
import io
import logging
import math
import time
import urllib.request
from decimal import Decimal
from typing import List, Optional

import mutagen
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
from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.desktop_dao import DesktopDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.team_dao import TeamDAO
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
    AssistantContactCreate,
    AssistantContactRead,
    AssistantContactRemoval,
    AssistantContactUpdate,
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
    delete_assistant_disk,
    delete_email,
    delete_phone_number,
    delete_pubsub_topic,
    get_running_jobs,
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

_prediction_owners: dict[str, str] = {}


def _build_assistant_read(
    a: Assistant,
    session: Session,
    *,
    api_key: Optional[str] = None,
    user_first_name: Optional[str] = None,
    user_last_name: Optional[str] = None,
    user_email: Optional[str] = None,
    user_image: Optional[str] = None,
    team_ids: Optional[List[int]] = None,
    contacts: Optional[list] = None,
    include_internal: bool = False,
) -> AssistantRead:
    """Build an ``AssistantRead`` from an ORM ``Assistant``.

    Contact fields (phone, email, whatsapp, etc.) are populated from the
    ``AssistantContact`` table rather than the legacy columns on the
    ``Assistant`` model.

    Args:
        contacts: Pre-fetched list of active ``AssistantContact`` rows for
            this assistant.  When ``None`` the contacts are fetched from
            the database.  Callers that build many ``AssistantRead``
            objects at once should batch-fetch contacts via
            ``AssistantContactDAO.get_active_contacts_for_assistants()`` and pass them in to
            avoid N+1 queries.
    """
    desktop_dao = DesktopDAO(session)
    user_desktop_url = None
    user_desktop_mode = None
    if a.user_desktop_id is not None:
        desktop = desktop_dao.get_by_id(a.user_desktop_id, a.user_id)
        if desktop:
            user_desktop_url = desktop.url
            user_desktop_mode = desktop.os

    if team_ids is None:
        if a.organization_id is not None:
            team_dao = TeamDAO(session)
            teams = team_dao.get_user_teams(a.user_id, a.organization_id)
            team_ids = [t.id for t in teams]
        else:
            team_ids = []

    # Resolve contact fields from AssistantContact rows
    if contacts is None:
        contact_dao = AssistantContactDAO(session)
        contacts = contact_dao.get_active_contacts_for_assistant(a.agent_id)

    contact_map: dict[str, object] = {}
    for c in contacts:
        contact_map[c.contact_type] = c

    phone_contact = contact_map.get("phone")
    email_contact = contact_map.get("email")
    whatsapp_contact = contact_map.get("whatsapp")

    return AssistantRead(
        agent_id=str(a.agent_id),
        user_id=a.user_id,
        organization_id=a.organization_id,
        deploy_env=a.deploy_env,
        first_name=a.first_name,
        surname=a.surname,
        age=a.age,
        nationality=a.nationality,
        profile_photo=a.profile_photo,
        profile_video=a.profile_video,
        desktop_mode=a.desktop_mode,
        user_desktop_id=a.user_desktop_id,
        user_desktop_filesys_sync=a.user_desktop_filesys_sync,
        user_desktop_url=user_desktop_url,
        user_desktop_mode=user_desktop_mode,
        about=a.about,
        phone_country=(phone_contact.country_code if phone_contact else None),
        weekly_limit=(float(a.weekly_limit) if a.weekly_limit is not None else None),
        max_parallel=a.max_parallel,
        created_at=a.created_at,
        updated_at=a.updated_at,
        phone=(phone_contact.contact_value if phone_contact else None),
        email=(email_contact.contact_value if email_contact else None),
        user_phone=(phone_contact.user_value if phone_contact else None),
        user_whatsapp_number=(
            whatsapp_contact.user_value if whatsapp_contact else None
        ),
        assistant_whatsapp_number=(
            whatsapp_contact.contact_value if whatsapp_contact else None
        ),
        voice_id=a.voice_id,
        voice_provider=a.voice_provider,
        timezone=a.timezone,
        demo_id=a.demo_id,
        is_local=a.is_local,
        monthly_spending_cap=(
            float(a.monthly_spending_cap)
            if a.monthly_spending_cap is not None
            else None
        ),
        desktop_filesync_sshkey=(
            a.desktop_filesync_sshkey if include_internal else None
        ),
        api_key=api_key,
        user_first_name=user_first_name,
        user_last_name=user_last_name,
        user_email=user_email,
        user_image=user_image,
        team_ids=team_ids,
    )


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

    # Base creation cost (contact provisioning costs are handled separately
    # via the dedicated POST /assistant/{id}/contact endpoint).
    total_creation_cost = settings.assistant_creation_cost

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
            desktop_mode=assistant_in.desktop_mode,
            user_desktop_id=assistant_in.user_desktop_id,
            user_desktop_filesys_sync=assistant_in.user_desktop_filesys_sync or False,
            about=assistant_in.about,
            weekly_limit=parsed_weekly_limit,
            max_parallel=assistant_in.max_parallel,
            voice_id=assistant_in.voice_id,
            voice_provider=assistant_in.voice_provider,
            timezone=assistant_in.timezone,
            organization_id=organization_id,
            is_local=assistant_in.is_local or False,
            deploy_env=assistant_in.deploy_env,
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
        # NOTE: Contact provisioning (phone, email, WhatsApp) is now handled
        # exclusively via the dedicated POST /assistant/{id}/contact endpoint.
        created_pubsub = None

        if assistant_in.create_infra:
            current_infra_step = "initializing"
            try:
                # Step 1: create pubsub topic
                current_infra_step = "create_pubsub_topic"
                pubsub_response = await create_pubsub_topic(
                    str(assistant_id),
                    deploy_env=assistant.deploy_env,
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

                if created_pubsub:
                    try:
                        await delete_pubsub_topic(
                            str(assistant_id),
                            deploy_env=assistant.deploy_env,
                        )
                    except Exception as e:
                        rollback_errors.append(
                            f"Failed to delete pubsub topic: {str(e)}",
                        )
                print(f"PUBSUB DELETED: {assistant_id}")

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

                error_msg = f"Infrastructure setup failed: {infra_error}"
                if rollback_errors:
                    error_msg += f" Rollback issues: {'; '.join(rollback_errors)}"
                logging.error(error_msg, exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Infrastructure setup failed",
                )

    except IntegrityError as e:
        session.rollback()
        logging.error(f"Database error creating assistant: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database error creating assistant",
        )
    except HTTPException:
        raise
    except Exception as e_prepare:
        logging.error(f"Failed to create assistant: {e_prepare}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create assistant",
        )

    # Phase 2: Deduct credits from the correct billing account (user or org).
    if not settings.is_staging:
        try:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(session, billing_entity, Decimal(str(total_creation_cost)))
            session.commit()
        except Exception as e_commit:
            logging.error(
                f"Payment processing failed for assistant creation: {e_commit}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Payment processing failed",
            )

    if assistant is None:
        # Should ideally not be reached if Phase 1 fails
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create assistant.",
        )

    # Phase 3: Wake up assistant (skip for local assistants -- unity runs locally)
    if not assistant_in.is_local:
        response = await wake_up_assistant(
            assistant.agent_id,
            deploy_env=assistant.deploy_env,
        )
        if response.status_code != 200:
            logging.error(f"Failed to wake up assistant: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to wake up assistant.",
            )
        else:
            print(f"ASSISTANT AWAKENED: {assistant.agent_id}")
    else:
        print(f"SKIPPED WAKEUP (local assistant): {assistant.agent_id}")

    # (Optional) Log pre-hire chat if provided
    if assistant_in.pre_hire_chat:
        try:
            # Convert Pydantic models to dictionaries for the webhook payload
            chat_messages = jsonable_encoder(assistant_in.pre_hire_chat)
            await log_pre_hire_chat(
                assistant_id=str(assistant.agent_id),
                messages=chat_messages,
                deploy_env=assistant.deploy_env,
            )
        except Exception as e_log:
            # We don't rollback the whole assistant creation for a logging failure,
            # but we should log it as a warning.
            logging.warning(
                f"Failed to log pre-hire chat for assistant {assistant.agent_id} via webhook. Error: {str(e_log)}",
            )

    # Phase 4: Prepare and return response
    return InfoResponse(
        info=_build_assistant_read(assistant, session),
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

        user_dao = UserDAO(session)
        users = {a.user_id: user_dao.get_by_id(a.user_id)[0] for a in assistants}

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        return InfoResponse(
            info=[
                _build_assistant_read(
                    a,
                    session,
                    user_first_name=(
                        users[a.user_id].name if users.get(a.user_id) else None
                    ),
                    user_last_name=(
                        users[a.user_id].last_name if users.get(a.user_id) else None
                    ),
                    user_email=users[a.user_id].email if users.get(a.user_id) else None,
                    user_image=users[a.user_id].image if users.get(a.user_id) else None,
                    contacts=contacts_by_assistant.get(a.agent_id, []),
                )
                for a in assistants
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching assistants: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error fetching assistants",
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
        # Look up the contact from the AssistantContact table
        contact_dao = AssistantContactDAO(session)
        contact = contact_dao.get_contact_by_assistant_and_type(
            assistant_id,
            contact_type,
        )

        if contact:
            # Deprovision external resource based on AssistantContact data
            if contact_type == "phone" and contact.contact_value:
                await delete_phone_number(
                    contact.contact_value,
                    deploy_env=assistant.deploy_env,
                )
            elif contact_type == "email" and contact.contact_value:
                await delete_email(
                    contact.contact_value,
                    deploy_env=assistant.deploy_env,
                )
            # WhatsApp: no external infra deletion needed

            # Soft-delete the AssistantContact row
            contact_dao.soft_delete_assistant_contact(
                assistant_id=assistant_id,
                contact_type=contact_type,
            )

        session.commit()
        session.refresh(assistant)
        updated_assistant = assistant

        # After successfully updating, trigger a reawaken
        try:
            await reawaken_assistant(
                str(updated_assistant.agent_id),
                deploy_env=updated_assistant.deploy_env,
            )
        except Exception as e:
            # Log the error but don't fail the request, as the main action succeeded
            logging.warning(
                f"Failed to reawaken assistant {updated_assistant.agent_id} after contact deletion: {e}",
            )

        return InfoResponse(
            info=_build_assistant_read(updated_assistant, session),
        )

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(
            f"Failed to delete contact for assistant {assistant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove contact",
        )


@router.post(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Create a contact detail for an assistant",
    description=(
        "Provisions external infrastructure (phone number, email, or WhatsApp sender) "
        "for the given assistant and creates a billing-tracked AssistantContact record. "
        "Deducts the one-time setup cost from credits."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact created successfully."},
        402: {"description": "Insufficient credits."},
        404: {"description": "Assistant not found."},
        409: {"description": "Contact type already exists for this assistant."},
    },
)
async def create_assistant_contact(
    assistant_id: int,
    contact_request: AssistantContactCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Create a new contact detail for an assistant.

    This endpoint:
    1. Checks that the assistant exists and the user has permission.
    2. Checks that no active contact of the same type already exists.
    3. Looks up the one-time and monthly costs from the AssistantContactCost table.
    4. Verifies the billing account has sufficient credits for the one-time cost.
    5. Provisions the external resource (Twilio number, Google Workspace email,
       WhatsApp sender).
    6. Creates an AssistantContact row and updates the backward-compat columns
       on the Assistant model.
    7. Deducts the one-time cost from credits.
    8. Triggers a reawaken so Unity picks up the new contact detail.

    If the database commit fails after provisioning, the external resource is
    rolled back (deprovisioned) to prevent resource leaks.
    """
    user_id = request.state.user_id
    organization_id = getattr(request.state, "organization_id", None)
    assistant_dao = AssistantDAO(session)

    # 1. Fetch and verify ownership
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

    # Permission check for org assistants
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

    contact_type = contact_request.contact_type
    contact_dao = AssistantContactDAO(session)

    # 2. Check for duplicate active contact
    existing = contact_dao.get_contact_by_assistant_and_type(assistant_id, contact_type)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active {contact_type} contact already exists for this assistant.",
        )

    # 3. Look up costs
    provider = None
    country_code = None
    if contact_type == "phone":
        provider = "twilio"
        country_code = contact_request.phone_country or "US"
    elif contact_type == "email":
        provider = "google_workspace"
    elif contact_type == "whatsapp":
        provider = "twilio"

    monthly_cost = contact_dao.get_contact_monthly_cost(
        contact_type,
        provider=provider,
        country_code=country_code,
    )
    one_time_cost = contact_dao.get_contact_one_time_cost(
        contact_type,
        provider=provider,
        country_code=country_code,
    )

    # 4. Credit check (skip in staging)
    if not settings.is_staging:
        try:
            billing_entity = get_billing_entity(session, user_id, organization_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Billing is not set up. Please add a payment method first.",
            )
        if one_time_cost > 0 and billing_entity.credits < one_time_cost:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Insufficient credits. Creating a {contact_type} contact "
                    f"requires ${one_time_cost} (setup fee)."
                ),
            )

    # 5. Provision external resource
    created_value = None
    user_value = None

    try:
        if contact_type == "phone":
            phone_country = contact_request.phone_country or "US"
            phone_response = await create_phone_number(
                phone_country=phone_country,
                deploy_env=assistant.deploy_env,
            )
            if "detail" in phone_response:
                raise Exception(
                    f"Phone number creation failed: {phone_response['detail']}",
                )
            created_value = phone_response.get("phoneNumber")
            user_value = contact_request.user_phone

        elif contact_type == "email":
            if not contact_request.email_local:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="email_local is required for email contacts.",
                )
            email_response = await create_email(
                contact_request.email_local,
                contact_request.first_name or "",
                contact_request.last_name or "",
                deploy_env=assistant.deploy_env,
            )
            if "detail" in email_response:
                raise Exception(
                    f"Email creation failed: {email_response['detail']}",
                )
            created_value = email_response.get("user", {}).get("primaryEmail")

            # Set up email watch
            await asyncio.sleep(10)
            watch_response = await watch_email(
                created_value,
                deploy_env=assistant.deploy_env,
            )
            if "detail" in watch_response:
                raise Exception(
                    f"Email watch setup failed: {watch_response['detail']}",
                )

        elif contact_type == "whatsapp":
            if not contact_request.user_whatsapp_number:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="user_whatsapp_number is required for WhatsApp contacts.",
                )
            whatsapp_response = await assign_whatsapp_sender(
                contact_request.user_whatsapp_number,
                deploy_env=assistant.deploy_env,
            )
            created_value = whatsapp_response.get("whatsapp_number")
            user_value = contact_request.user_whatsapp_number

        if not created_value:
            raise Exception(f"Failed to provision {contact_type}: no value returned.")

    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Failed to provision {contact_type} for assistant {assistant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to provision {contact_type}",
        )

    # 6. Create AssistantContact row + update Assistant columns + deduct cost
    #    Wrap in try/except to rollback the external provisioning if DB fails.
    try:
        # Refresh session after potentially long infra operations
        session.close()
        session = next(get_db_session(request))
        assistant_dao = AssistantDAO(session)

        # Re-fetch assistant with fresh session
        assistant = assistant_dao.get_assistant_by_id(
            user_id=user_id,
            agent_id=assistant_id,
            organization_id=organization_id,
        )
        if not assistant:
            raise Exception("Assistant no longer exists after provisioning.")

        # Create AssistantContact row
        contact = contact_dao.upsert_assistant_contact(
            assistant_id=assistant_id,
            contact_type=contact_type,
            contact_value=created_value,
            provider=provider,
            country_code=country_code,
            user_value=user_value,
        )
        contact.monthly_cost = monthly_cost

        # 7. Deduct one-time cost
        if not settings.is_staging and one_time_cost > 0:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            deduct_credits(session, billing_entity, one_time_cost)

        session.commit()

    except Exception as db_error:
        session.rollback()
        logging.error(
            f"DB commit failed after provisioning {contact_type} for assistant "
            f"{assistant_id}: {db_error}. Rolling back external resource.",
        )
        # Rollback the external resource
        try:
            if contact_type == "phone":
                await delete_phone_number(
                    created_value,
                    deploy_env=assistant.deploy_env,
                )
            elif contact_type == "email":
                await delete_email(
                    created_value,
                    deploy_env=assistant.deploy_env,
                )
            # WhatsApp: no explicit deprovisioning needed
        except Exception as rollback_error:
            logging.error(
                f"RESOURCE LEAK: Failed to rollback {contact_type} "
                f"'{created_value}' after DB failure: {rollback_error}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save {contact_type} contact: {str(db_error)}",
        )

    # 8. Trigger reawaken so Unity picks up the new contact
    try:
        await reawaken_assistant(
            str(assistant_id),
            deploy_env=assistant.deploy_env,
        )
    except Exception as e:
        logging.warning(
            f"Failed to reawaken assistant {assistant_id} after contact creation: {e}",
        )

    # Re-fetch for response
    assistant = assistant_dao.get_assistant_by_id(
        user_id=user_id,
        agent_id=assistant_id,
        organization_id=organization_id,
    )
    return InfoResponse(
        info=_build_assistant_read(assistant, session),
    )


@router.get(
    "/assistant/{assistant_id}/contacts",
    response_model=InfoResponse[list[AssistantContactRead]],
    status_code=status.HTTP_200_OK,
    summary="List active contact details for an assistant",
    description="Returns all active (non-deleted) contact details with billing metadata.",
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact details returned successfully."},
        404: {"description": "Assistant not found."},
    },
)
async def list_assistant_contacts(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[list[AssistantContactRead]]:
    """
    List all active contact details for an assistant.

    Returns each contact with its billing metadata (monthly cost,
    status, grace period info, etc.).
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

    if organization_id is not None:
        ra_dao = ResourceAccessDAO(session)
        if not ra_dao.check_user_permission(
            user_id,
            "assistant",
            assistant_id,
            "assistant:read",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view this assistant's contacts.",
            )

    contact_dao = AssistantContactDAO(session)
    contacts = contact_dao.get_active_contacts_for_assistant(assistant_id)
    contact_reads = [
        AssistantContactRead(
            id=c.id,
            assistant_id=c.assistant_id,
            contact_type=c.contact_type,
            contact_value=c.contact_value,
            provider=c.provider,
            provisioned_by=c.provisioned_by,
            country_code=c.country_code,
            user_value=c.user_value,
            status=c.status,
            monthly_cost=float(c.monthly_cost) if c.monthly_cost is not None else None,
            created_at=c.created_at,
            updated_at=c.updated_at,
            grace_period_started_at=c.grace_period_started_at,
        )
        for c in contacts
    ]
    return InfoResponse(info=contact_reads)


@router.put(
    "/assistant/{assistant_id}/contact",
    response_model=InfoResponse[AssistantRead],
    status_code=status.HTTP_200_OK,
    summary="Update contact metadata",
    description=(
        "Updates non-provisioned fields (user_value, metadata) on an existing contact. "
        "Changing the actual provisioned resource requires delete + create."
    ),
    tags=["Assistant Management"],
    responses={
        200: {"description": "Contact updated successfully."},
        404: {"description": "Assistant or contact not found."},
    },
)
async def update_assistant_contact(
    assistant_id: int,
    contact_update: AssistantContactUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[AssistantRead]:
    """
    Update non-provisioned fields on an existing contact.

    Only ``user_value`` and ``metadata`` can be changed via this endpoint.
    Changing the actual provisioned resource (phone number, email address, etc.)
    requires a delete + create flow.
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

    # Permission check for org assistants
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

    contact_type = contact_update.contact_type
    contact_dao = AssistantContactDAO(session)
    contact = contact_dao.get_contact_by_assistant_and_type(assistant_id, contact_type)
    if not contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active {contact_type} contact found for this assistant.",
        )

    # Update user_value
    if contact_update.user_value is not None:
        contact.user_value = contact_update.user_value

    # Update metadata (merge with existing)
    if contact_update.metadata is not None:
        existing_meta = contact.metadata_ or {}
        contact.metadata_ = {**existing_meta, **contact_update.metadata}

    session.commit()
    session.refresh(assistant)

    # Trigger reawaken so Unity picks up the user_value change
    try:
        await reawaken_assistant(
            str(assistant_id),
            deploy_env=assistant.deploy_env,
        )
    except Exception as e:
        logging.warning(
            f"Failed to reawaken assistant {assistant_id} after contact update: {e}",
        )

    return InfoResponse(
        info=_build_assistant_read(assistant, session),
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
            response = await stop_jobs(
                assistant_id,
                deploy_env=assistant.deploy_env,
            )
            print(f"JOB STOPPED: {response['job_names']}")
        except Exception as e:
            logging.error(f"Failed to stop job: {str(e)}")
            cleanup_errors.append(f"Failed to stop job: {str(e)}")

        # DB operations (fast, sequential, session-bound)
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

        # Fetch active contacts before parallel cleanup (DB read)
        contact_dao = AssistantContactDAO(session)
        try:
            active_contacts = contact_dao.get_active_contacts_for_assistant(
                assistant_id,
            )
        except Exception as e:
            active_contacts = []
            cleanup_errors.append(f"Failed to fetch assistant contacts: {str(e)}")

        # Parallel infrastructure cleanup (all independent after stop_jobs)
        async def _cleanup_disk():
            # Delete persistent disk if assistant uses a pool VM
            if assistant.desktop_mode in ("windows", "ubuntu"):
                try:
                    await delete_assistant_disk(
                        str(assistant_id),
                        deploy_env=assistant.deploy_env,
                    )
                except Exception as e:
                    logging.error(f"Failed to delete assistant disk: {str(e)}")
                    cleanup_errors.append(f"Failed to delete assistant disk: {str(e)}")

        async def _cleanup_gcs():
            # Delete GCS profile photo if it exists and is a GCS URL from the assistant images bucket
            if assistant.profile_photo and assistant.profile_photo.startswith("gs://"):
                try:
                    deleted_from_gcs = await asyncio.to_thread(
                        bucket_service.delete_assistant_file,
                        assistant.profile_photo,
                    )
                    if not deleted_from_gcs:
                        logging.error(
                            f"Profile photo {assistant.profile_photo} for assistant {assistant_id} was not deleted from GCS.",
                        )
                        cleanup_errors.append(
                            f"Failed to delete profile photo from GCS",
                        )
                except Exception as e_gcs:
                    logging.error(
                        f"Failed to delete profile photo {assistant.profile_photo} for assistant {assistant_id}: {str(e_gcs)}",
                    )
                    cleanup_errors.append(
                        f"Failed to delete profile photo: {str(e_gcs)}",
                    )

            # Delete GCS profile video if it exists
            if assistant.profile_video and assistant.profile_video.startswith("gs://"):
                try:
                    deleted_from_gcs = await asyncio.to_thread(
                        bucket_service.delete_assistant_file,
                        assistant.profile_video,
                    )
                    if not deleted_from_gcs:
                        logging.error(
                            f"Profile video {assistant.profile_video} for assistant {assistant_id} was not deleted from GCS.",
                        )
                        cleanup_errors.append(
                            f"Failed to delete profile video from GCS",
                        )
                except Exception as e_gcs:
                    logging.error(
                        f"Failed to delete profile video {assistant.profile_video} for assistant {assistant_id}: {str(e_gcs)}",
                    )
                    cleanup_errors.append(
                        f"Failed to delete profile video: {str(e_gcs)}",
                    )

            # Delete all assistant GCS data (recordings, media, attachments) under {assistant_id}/
            try:
                cleanup_counts = await asyncio.to_thread(
                    bucket_service.delete_all_assistant_data,
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

        async def _cleanup_pubsub():
            # Delete pubsub topic
            try:
                await delete_pubsub_topic(
                    str(assistant_id),
                    deploy_env=assistant.deploy_env,
                )
            except Exception as e:
                cleanup_errors.append(f"Failed to delete pubsub topic: {str(e)}")
            print(f"PUBSUB DELETED: {assistant_id}")

        async def _deprovision_contacts():
            # Deprovision all contacts (phone numbers, emails)
            async def _deprovision(ac):
                try:
                    if ac.contact_type == "phone" and ac.contact_value:
                        await delete_phone_number(
                            ac.contact_value,
                            deploy_env=assistant.deploy_env,
                        )
                        print(f"PHONE DELETED: {ac.contact_value}")
                    elif ac.contact_type == "email" and ac.contact_value:
                        await delete_email(
                            ac.contact_value,
                            deploy_env=assistant.deploy_env,
                        )
                        print(f"EMAIL DELETED: {ac.contact_value}")
                except Exception as e:
                    cleanup_errors.append(
                        f"Failed to deprovision {ac.contact_type} "
                        f"({ac.contact_value}): {str(e)}",
                    )

            await asyncio.gather(*[_deprovision(ac) for ac in active_contacts])

        await asyncio.gather(
            _cleanup_disk(),
            _cleanup_gcs(),
            _cleanup_pubsub(),
            _deprovision_contacts(),
        )

        # DB finalization (fast, sequential, session-bound)
        # Soft-delete all contacts from AssistantContact table
        try:
            contact_dao.soft_delete_all_contacts_for_assistant(assistant_id)
        except Exception as e:
            cleanup_errors.append(f"Failed to clean up assistant contacts: {str(e)}")

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

        # Finally delete the assistant record
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

    try:
        weekly_limit: Optional[Decimal] = None
        if update.weekly_limit is not None:
            weekly_limit = Decimal(update.weekly_limit)

        # NOTE: Contact provisioning (phone, email, WhatsApp) has been removed
        # from this endpoint.  Use POST /assistant/{id}/contact instead.
        # Deprecated contact fields in the request body are silently excluded.
        _DEPRECATED_CONTACT_FIELDS = {
            "email",
            "phone",
            "user_phone",
            "phone_country",
            "user_whatsapp_number",
            "create_infra",
        }

        update_data = update.model_dump(exclude_unset=True)
        # Remove deprecated contact fields
        for field_name in _DEPRECATED_CONTACT_FIELDS:
            update_data.pop(field_name, None)
        if "weekly_limit" in update_data and update.weekly_limit is not None:
            update_data["weekly_limit"] = Decimal(update.weekly_limit)
        if (
            "monthly_spending_cap" in update_data
            and update.monthly_spending_cap is not None
        ):
            update_data["monthly_spending_cap"] = Decimal(
                str(update.monthly_spending_cap),
            )

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

        return InfoResponse(
            info=_build_assistant_read(updated, session),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logging.error(f"Error updating assistant config: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error updating assistant config",
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

    # Block transfer if the assistant has contacts in grace_period
    # (unpaid billing must be resolved before transferring ownership)
    contact_dao = AssistantContactDAO(session)
    if contact_dao.has_grace_period_contacts(assistant_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot transfer assistant: it has contact details in a billing "
                "grace period. Please add credits to resolve the outstanding "
                "balance before transferring."
            ),
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
        logging.error(
            f"Failed to transfer assistant {assistant_id} to org: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to transfer assistant",
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

    # Block transfer if the assistant has contacts in grace_period
    # (unpaid billing must be resolved before transferring ownership)
    contact_dao = AssistantContactDAO(session)
    if contact_dao.has_grace_period_contacts(assistant_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot transfer assistant: it has contact details in a billing "
                "grace period. Please add credits to resolve the outstanding "
                "balance before transferring."
            ),
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
        logging.error(
            f"Failed to transfer assistant {assistant_id} to personal: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to transfer assistant",
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
        logging.error(f"Database error registering voice: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database error registering voice",
        )
    except HTTPException as e:
        session.rollback()
        raise e
    except Exception as e:
        session.rollback()
        logging.error(f"Error registering voice: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error registering voice",
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

    MAX_VOICE_CLONE_BYTES = 25 * 1024 * 1024  # 25 MB
    try:
        file_content = await file.read()
        if len(file_content) > MAX_VOICE_CLONE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File size exceeds {MAX_VOICE_CLONE_BYTES // (1024 * 1024)}MB limit.",
            )
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
        logging.error(
            f"Failed to save cloned voice to database: {e_db_integrity}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Failed to save cloned voice to database",
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
        logging.error(f"Failed to clone and save voice: {e_generic}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clone and save voice",
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
        logging.error(f"Error fetching user voices: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error fetching user voices",
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
            f"Generic error during voice deletion {voice_id}: {e_generic}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting voice",
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
            f"Unexpected error generating speech for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate speech",
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
            f"Unexpected error generating voice previews for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate voice previews",
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
        logging.error(
            f"Database error creating voice from preview: {e_db}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database error creating voice, it might already exist",
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        if new_el_voice_id:
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
            f"Unexpected error creating voice from preview for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create voice from preview",
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
    assistant_id: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
):
    bucket_service = BucketService()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if assistant_id is not None:
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

    ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if not file.content_type or file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_IMAGE_TYPES)}",
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
            assistant_id=assistant_id,
        )
        return InfoResponse(info=AssistantPhotoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(f"Error uploading assistant photo for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not upload photo",
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
    assistant_id: Optional[int] = Form(None),
    session: Session = Depends(get_db_session),
):
    bucket_service = BucketService()
    user_id = request.state.user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not authenticated.",
        )

    if assistant_id is not None:
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

    ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
    if not file.content_type or file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_VIDEO_TYPES)}",
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
            assistant_id=assistant_id,
        )
        return InfoResponse(info=AssistantVideoUploadResponse(gcs_url=gcs_url))
    except HTTPException as e:
        raise e
    except Exception as e:
        logging.error(
            f"Error uploading assistant video for user {user_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not upload video",
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
        logging.error(f"Error generating photo for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate photo",
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
        logging.error(f"Error editing photo for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not edit photo",
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

        # Process audio input and capture raw bytes for duration computation
        audio_bytes_for_duration: Optional[bytes] = None

        if is_audio_file_provided:
            if not audio_file.content_type or not audio_file.content_type.startswith(
                "audio/",
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file type for 'audio_file'. Only audio files are allowed.",
                )
            audio_content = await audio_file.read()
            audio_bytes_for_duration = audio_content
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
            from orchestra.web.api.utils.url_validation import validate_url_for_ssrf

            validate_url_for_ssrf(audio_url)
            final_audio_url_for_replicate = audio_url
            try:
                with urllib.request.urlopen(audio_url, timeout=30) as resp:
                    audio_bytes_for_duration = resp.read()
            except Exception as e:
                logging.warning(
                    f"Could not download audio from URL to compute duration: {e}",
                )

        if not final_image_url_for_replicate or not final_audio_url_for_replicate:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing valid image or audio input for Replicate.",
            )

        # Derive billable duration from the actual audio
        audio_duration_seconds: float = float(settings.default_video_duration)
        if audio_bytes_for_duration:
            try:
                audio_file_obj = mutagen.File(io.BytesIO(audio_bytes_for_duration))
                if audio_file_obj is not None and audio_file_obj.info is not None:
                    audio_duration_seconds = audio_file_obj.info.length
            except Exception as e:
                logging.warning(
                    f"Could not compute audio duration, "
                    f"falling back to {settings.default_video_duration}s: {e}",
                )

        billable_duration = math.ceil(audio_duration_seconds)

        # OmniHuman 1.5 supports audio up to 35s
        if audio_duration_seconds > 35:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Audio duration exceeds the 35 second limit for photo animation.",
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

        # Pre-check credits
        if not settings.is_staging:
            try:
                billing_entity = get_billing_entity(session, user_id, organization_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Billing is not set up. Please add a payment method first.",
                )
            video_cost = settings.video_generation_cost * billable_duration
            if billing_entity.credits < video_cost:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Insufficient credits to generate video.",
                )

        prediction = replicate_service.create_video_animation(
            image_url=final_image_url_for_replicate,
            audio_url=final_audio_url_for_replicate,
            seed=seed,
        )

        _prediction_owners[prediction.id] = user_id

        # Deduct credits after successful prediction creation
        if not settings.is_staging:
            from orchestra.lib.billing import deduct_credits

            billing_entity = get_billing_entity(session, user_id, organization_id)
            video_cost = settings.video_generation_cost * billable_duration
            deduct_credits(
                session,
                billing_entity,
                Decimal(str(video_cost)),
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
        logging.error(f"Error animating video for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not animate video",
        )
    finally:
        # NOTE: Do NOT delete temp files here. The prediction runs
        # asynchronously on Replicate and needs to download these files
        # after the endpoint returns. Temp files in the ``tmp/`` folder
        # are cleaned up by a scheduled job (see temp_file_cleanup routine).
        pass


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
    user_id = request.state.user_id
    owner = _prediction_owners.get(prediction_id)
    if owner is not None and owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found.",
        )
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
    user_id = request.state.user_id
    owner = _prediction_owners.get(prediction_id)
    if owner is not None and owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prediction not found.",
        )
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
async def admin_get_assistant_status(
    assistant_id: str,
    request: Request,
) -> InfoResponse[AssistantStatus]:
    """
    Get the live status of an assistant's dedicated service.
    """
    try:
        job_names = await get_running_jobs(assistant_id)
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

    if request_body.desktop_filesync_sshkey is not None:
        assistant.desktop_filesync_sshkey = request_body.desktop_filesync_sshkey
        updated_fields.append("desktop_filesync_sshkey")

    if request_body.deploy_env is not None:
        assistant.deploy_env = request_body.deploy_env
        updated_fields.append("deploy_env")

    if not updated_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update. Provide at least one field.",
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
        "expensive lookups (api_key, user info) when those fields aren't requested.",
        example="email,agent_id,first_name",
    ),
    session: Session = Depends(get_db_session),
):
    """
    List all assistants in the system with optional filtering and field selection.

    When 'from_fields' is specified, returns only the requested fields, skipping expensive
    database lookups for unrequested fields like api_key and user details.

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
                keys = api_key_dao.get_organization_keys(
                    assistant.user_id,
                    assistant.organization_id,
                )
            return keys[0][0].key if keys else None

        # Perform expensive lookups only if needed
        api_keys = (
            [get_api_key_for_assistant(a) for a in assistants]
            if (requested_fields is None or "api_key" in requested_fields)
            else None
        )
        users = (
            [user_dao.get_by_id(a.user_id)[0] for a in assistants]
            if (
                requested_fields is None
                or bool(
                    requested_fields
                    & {"user_email", "user_first_name", "user_last_name", "user_image"},
                )
            )
            else None
        )

        skip_teams = requested_fields is not None and "team_ids" not in requested_fields

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        # Build AssistantRead objects
        assistant_reads = [
            _build_assistant_read(
                a,
                session,
                api_key=api_keys[i] if api_keys else None,
                user_first_name=users[i].name if users else None,
                user_last_name=users[i].last_name if users else None,
                user_email=users[i].email if users else None,
                user_image=users[i].image if users else None,
                team_ids=[] if skip_teams else None,
                contacts=contacts_by_assistant.get(a.agent_id, []),
                include_internal=True,
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

    # Update the whatsapp AssistantContact row (the canonical source of
    # contact data) instead of the legacy columns on the Assistant model.
    contact_dao = AssistantContactDAO(session)
    whatsapp_contact = contact_dao.get_contact_by_assistant_and_type(
        a.agent_id,
        "whatsapp",
    )
    if whatsapp_contact:
        if new_assistant_whatsapp_number:
            whatsapp_contact.contact_value = new_assistant_whatsapp_number
        if new_user_whatsapp_number:
            whatsapp_contact.user_value = new_user_whatsapp_number
    else:
        # No existing whatsapp contact – create one if a new number was provided
        if new_assistant_whatsapp_number:
            contact_dao.upsert_assistant_contact(
                assistant_id=a.agent_id,
                contact_type="whatsapp",
                contact_value=new_assistant_whatsapp_number,
                user_value=new_user_whatsapp_number,
            )

    session.commit()

    # Get API key based on assistant type (personal vs organizational)
    if a.organization_id is None:
        keys = api_key_dao.get_personal_keys(a.user_id)
    else:
        keys = api_key_dao.get_organization_keys(
            a.user_id,
            a.organization_id,
        )
    api_key = keys[0][0].key if keys else None

    # Return updated assistant
    return InfoResponse(
        info=_build_assistant_read(
            a,
            session,
            api_key=api_key,
            include_internal=True,
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
                keys = api_key_dao.get_organization_keys(
                    assistant.user_id,
                    assistant.organization_id,
                )
            return keys[0][0].key if keys else None

        api_keys = [get_api_key_for_assistant(a) for a in assistants]

        # Batch-fetch contacts for all assistants (avoids N+1 queries)
        contact_dao = AssistantContactDAO(session)
        all_contacts = contact_dao.get_active_contacts_for_assistants(
            [a.agent_id for a in assistants],
        )
        contacts_by_assistant: dict[int, list] = {}
        for c in all_contacts:
            contacts_by_assistant.setdefault(c.assistant_id, []).append(c)

        return InfoResponse(
            info=[
                _build_assistant_read(
                    a,
                    session,
                    api_key=api_keys[i],
                    contacts=contacts_by_assistant.get(a.agent_id, []),
                    include_internal=True,
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
    tags=["Assistant Management"],
    summary="Get assistant spending limit",
    description="Get the monthly spending limit for an assistant.",
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


@router.get("/assistant/{agent_id}/spend", response_model=AssistantSpendResponse)
async def get_assistant_spend(
    request: Request,
    agent_id: int,
    month: str = Query(
        ...,
        description="Month in YYYY-MM format",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-01"],
    ),
    session: Session = Depends(get_db_session),
):
    """Get an assistant's cumulative spend for a given month."""
    user_id = request.state.user_id

    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(agent_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found.")

    if assistant.user_id != user_id:
        raise HTTPException(status_code=404, detail="Assistant not found.")

    cumulative_spend = assistant_dao.get_cumulative_spend(agent_id, month)
    limit = assistant_dao.get_spending_cap(agent_id)

    percent_used = None
    if limit is not None and limit > 0:
        percent_used = round((cumulative_spend / limit) * 100, 2)

    credit_balance = None
    if assistant.organization_id is not None:
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


@router.put(
    "/assistant/{agent_id}/spending-limit",
    response_model=AssistantSpendingLimitResponse,
    tags=["Assistant Management"],
    summary="Set assistant spending limit",
    description="Set or update the monthly spending limit for an assistant.",
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
            # Demo-specific settings
            timezone="UTC",  # Default timezone for demos
            monthly_spending_cap=Decimal(str(demo_create.monthly_spending_cap)),
            # Link to demo metadata
            demo_id=demo_meta.id,
            deploy_env=source_assistant.deploy_env,
        )
        session.add(demo_assistant)
        session.flush()  # Get the agent_id

        # Provision phone infrastructure
        # Use provided phone_country, fallback to source assistant's country, then default to US
        phone_country = demo_create.phone_country or "US"
        demo_phone_number = None
        try:
            phone_response = await create_phone_number(
                phone_country=phone_country,
                deploy_env=demo_assistant.deploy_env,
            )
            if "detail" in phone_response:
                raise Exception(f"Phone creation failed: {phone_response['detail']}")
            demo_phone_number = phone_response.get("phoneNumber")
        except Exception as e:
            logging.error(f"Failed to provision phone for demo assistant: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to provision phone number: {str(e)}",
            )

        # Optionally provision email infrastructure
        demo_email = None
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
                    deploy_env=demo_assistant.deploy_env,
                )
                if "detail" in email_response:
                    raise Exception(
                        f"Email creation failed: {email_response['detail']}",
                    )
                demo_email = email_response.get("user", {}).get("primaryEmail")
                logging.info(f"Email provisioned for demo assistant: {demo_email}")

                # Wait and set up email watch
                await asyncio.sleep(10)
                watch_response = await watch_email(
                    demo_email,
                    deploy_env=demo_assistant.deploy_env,
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
                deploy_env=demo_assistant.deploy_env,
            )
        except Exception as e:
            logging.warning(f"Failed to create pubsub topic for demo assistant: {e}")

        # Create AssistantContact rows for demo assistant
        contact_dao = AssistantContactDAO(session)
        if demo_phone_number:
            contact_dao.upsert_assistant_contact(
                assistant_id=demo_assistant.agent_id,
                contact_type="phone",
                contact_value=demo_phone_number,
                provider="twilio",
                country_code=phone_country,
                user_value=demo_create.demoer_phone,
            )
        if demo_email:
            contact_dao.upsert_assistant_contact(
                assistant_id=demo_assistant.agent_id,
                contact_type="email",
                contact_value=demo_email,
                provider="google_workspace",
            )

        # Commit the transaction BEFORE waking up the assistant
        # This ensures the assistant is visible to Adapters when it queries Orchestra
        session.commit()

        # Wake up the assistant with demo mode
        # This must happen AFTER commit so Adapters can find the assistant in the database
        try:
            await wake_up_assistant(
                str(demo_assistant.agent_id),
                deploy_env=demo_assistant.deploy_env,
            )
        except Exception as e:
            logging.warning(f"Failed to wake up demo assistant: {e}")

        return InfoResponse(
            info=_build_assistant_read(demo_assistant, session),
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
