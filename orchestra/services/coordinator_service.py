"""Coordinator provisioning and lifecycle helpers."""

from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Context,
    LogEvent,
    LogEventContext,
    Project,
    Space,
)
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal

ASSISTANTS_PROJECT_NAME = "Assistants"
COORDINATOR_CONTEXT_PREFIX = "Coordinator"
COORDINATOR_RESET_CONTEXTS = (
    "Coordinator/State",
    "Coordinator/Checklist",
    "Transcripts",
    "Exchanges",
)
COORDINATOR_STATE_CONTEXT = "Coordinator/State"
COORDINATOR_TRANSCRIPTS_CONTEXT = "Transcripts"
COORDINATOR_ADMIN_ROLES = {"Owner", "Admin"}
CoordinatorMode = Literal["active", "skipped", "ready_to_go"]


def get_personal_coordinator(session: Session, user_id: str) -> Assistant | None:
    """Return the user's personal Coordinator when one already exists."""
    return session.scalar(
        select(Assistant).where(
            Assistant.user_id == user_id,
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        ),
    )


def get_org_coordinator(session: Session, organization_id: int) -> Assistant | None:
    """Return the organization's Coordinator when one already exists."""
    return session.scalar(
        select(Assistant).where(
            Assistant.organization_id == organization_id,
            Assistant.is_coordinator.is_(True),
        ),
    )


def pubsub_topic_response_failed(response: dict) -> bool:
    """Return whether a Comms topic-provisioning response is a failure."""
    return bool(
        response.get("detail")
        or response.get("error")
        or response.get("success") is False,
    )


def create_coordinator_assistant(
    session: Session,
    *,
    owner_user_id: str,
    organization_id: int | None,
    timezone: str | None = None,
) -> Assistant:
    """Create the Coordinator assistant row for a personal or org scope."""
    assistant = AssistantDAO(session).create_assistant(
        user_id=owner_user_id,
        first_name="Coordinator",
        surname=None,
        age=None,
        nationality=None,
        profile_photo=None,
        profile_video=None,
        desktop_mode=None,
        user_desktop_id=None,
        user_desktop_filesys_sync=False,
        about="Coordinates setup and shared assistant memory.",
        weekly_limit=None,
        max_parallel=None,
        voice_id=None,
        voice_provider=None,
        timezone=timezone,
        organization_id=organization_id,
        is_local=False,
        is_coordinator=True,
        deploy_env=None,
        job_title="Coordinator",
    )
    session.flush()
    return assistant


def ensure_assistants_project(
    session: Session,
    *,
    owner_user_id: str,
    organization_id: int | None,
) -> Project:
    """Ensure the scope has the durable Assistants project."""
    if organization_id is None:
        project = session.scalar(
            select(Project).where(
                Project.user_id == owner_user_id,
                Project.organization_id.is_(None),
                Project.name == ASSISTANTS_PROJECT_NAME,
            ),
        )
        if project is None:
            project = Project(
                user_id=owner_user_id,
                organization_id=None,
                name=ASSISTANTS_PROJECT_NAME,
                description="Project to manage and track all your assistants.",
                is_versioned=False,
            )
            session.add(project)
            session.flush()
        return project

    project = session.scalar(
        select(Project).where(
            Project.organization_id == organization_id,
            Project.name == ASSISTANTS_PROJECT_NAME,
        ),
    )
    if project is None:
        project = Project(
            user_id=None,
            organization_id=organization_id,
            name=ASSISTANTS_PROJECT_NAME,
            description="Project to manage and track all organization assistants.",
            is_versioned=False,
        )
        session.add(project)
        session.flush()
        grant_project_access_to_org_members(
            session,
            project=project,
            owner_user_id=owner_user_id,
            organization_id=organization_id,
        )
    return project


def grant_project_access_to_org_members(
    session: Session,
    *,
    project: Project,
    owner_user_id: str,
    organization_id: int,
) -> None:
    """Grant Owner/Member project access for current organization members."""
    resource_access_dao = ResourceAccessDAO(session)
    role_dao = RoleDAO(session)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)
    if owner_role is not None:
        resource_access_dao.grant_access(
            resource_type="project",
            resource_id=project.id,
            role_id=owner_role.id,
            grantee_type="user",
            grantee_id=owner_user_id,
        )

    member_role = role_dao.get_by_name("Member", organization_id=None)
    if member_role is None:
        return

    org_members = OrganizationMemberDAO(session).filter(organization_id=organization_id)
    for member_row in org_members:
        member = member_row[0]
        if member.user_id == owner_user_id:
            continue
        resource_access_dao.grant_access(
            resource_type="project",
            resource_id=project.id,
            role_id=member_role.id,
            grantee_type="user",
            grantee_id=member.user_id,
        )


def grant_owner_access_to_assistant(
    session: Session,
    *,
    assistant: Assistant,
    owner_user_id: str,
) -> None:
    """Grant the owner role on an org assistant resource."""
    owner_role = RoleDAO(session).get_by_name("Owner", organization_id=None)
    if owner_role is None:
        return
    ResourceAccessDAO(session).grant_access(
        resource_type="assistant",
        resource_id=assistant.agent_id,
        role_id=owner_role.id,
        grantee_type="user",
        grantee_id=owner_user_id,
    )


def ensure_org_default_space(
    session: Session,
    *,
    organization_id: int,
    owner_user_id: str,
    assistant: Assistant,
    name: str = "Organization Default",
) -> Space:
    """Ensure the organization has its default Coordinator memory space."""
    space = session.scalar(
        select(Space).where(
            Space.organization_id == organization_id,
            Space.kind == "org_default",
        ),
    )
    if space is None:
        space = Space(
            name=name,
            description="Default shared memory for the organization Coordinator.",
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            kind="org_default",
        )
        session.add(space)
        session.flush()

    membership = session.scalar(
        select(AssistantSpaceMembership).where(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == space.space_id,
        ),
    )
    if membership is None:
        session.add(
            AssistantSpaceMembership(
                assistant_id=assistant.agent_id,
                space_id=space.space_id,
                added_by=owner_user_id,
            ),
        )
        session.flush()
    return space


def create_personal_coordinator(session: Session, user_id: str) -> Assistant:
    """Create or return the user's personal Coordinator."""
    existing = get_personal_coordinator(session, user_id)
    if existing is not None:
        return existing

    assistant = create_coordinator_assistant(
        session,
        owner_user_id=user_id,
        organization_id=None,
    )
    ensure_assistants_project(
        session,
        owner_user_id=user_id,
        organization_id=None,
    )
    return assistant


def create_organization_coordinator(
    session: Session,
    *,
    owner_user_id: str,
    organization_id: int,
    timezone: str | None,
    space_name: str = "Organization Default",
) -> Assistant:
    """Create or return the organization's Coordinator and default space."""
    existing = get_org_coordinator(session, organization_id)
    if existing is not None:
        ensure_org_default_space(
            session,
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            assistant=existing,
            name=space_name,
        )
        return existing

    assistant = create_coordinator_assistant(
        session,
        owner_user_id=owner_user_id,
        organization_id=organization_id,
        timezone=timezone,
    )
    grant_owner_access_to_assistant(
        session,
        assistant=assistant,
        owner_user_id=owner_user_id,
    )
    ensure_assistants_project(
        session,
        owner_user_id=owner_user_id,
        organization_id=organization_id,
    )
    ensure_org_default_space(
        session,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        assistant=assistant,
        name=space_name,
    )
    return assistant


def require_authorized_coordinator(
    session: Session,
    *,
    coordinator_id: int,
    user_id: str,
) -> Assistant:
    """Resolve a Coordinator and enforce write plus privileged lifecycle access."""
    coordinator = AssistantDAO(session).get_assistant_by_agent_id(
        agent_id=coordinator_id,
    )
    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Coordinator not found.",
        )
    if not coordinator.is_coordinator:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="not_a_coordinator",
        )

    if coordinator.organization_id is None:
        resource_access_dao = ResourceAccessDAO(session)
        if not resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            coordinator.agent_id,
            "assistant:write",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this Coordinator.",
            )
        if coordinator.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this Coordinator.",
            )
        return coordinator

    resource_access_dao = ResourceAccessDAO(session)
    has_write_permission = resource_access_dao.check_user_permission(
        user_id,
        "assistant",
        coordinator.agent_id,
        "assistant:write",
    ) or resource_access_dao.check_org_member_permission(
        user_id,
        coordinator.organization_id,
        "assistant:write",
    )
    if not has_write_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this Coordinator.",
        )

    member = OrganizationMemberDAO(session).get_member_with_details(
        user_id=user_id,
        organization_id=coordinator.organization_id,
    )
    if member is None or member.get("role_name") not in COORDINATOR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_required",
        )
    return coordinator


def _project_for_coordinator(session: Session, coordinator: Assistant) -> Project:
    return ensure_assistants_project(
        session,
        owner_user_id=coordinator.user_id,
        organization_id=coordinator.organization_id,
    )


def _coordinator_context_name(coordinator: Assistant, suffix: str) -> str:
    return f"{coordinator.user_id}/{coordinator.agent_id}/{suffix}"


def _lock_coordinator_context(
    session: Session,
    *,
    coordinator: Assistant,
    suffix: str,
) -> None:
    """Serialize first-write races for one Coordinator context."""
    lock_key = f"coordinator:{coordinator.agent_id}:{suffix}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )


def _get_context(
    session: Session,
    *,
    project_id: int,
    context_name: str,
) -> Context | None:
    return session.scalar(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == context_name,
        ),
    )


def _ensure_context(
    session: Session,
    *,
    project_id: int,
    context_name: str,
) -> Context:
    context = _get_context(
        session,
        project_id=project_id,
        context_name=context_name,
    )
    if context is None:
        context = Context(
            project_id=project_id,
            name=context_name,
            is_versioned=False,
            allow_duplicates=True,
        )
        session.add(context)
        session.flush()
    return context


def _build_project_dao(session: Session) -> ProjectDAO:
    context_dao = ContextDAO(session)
    return ProjectDAO(
        session,
        organization_member_dao=OrganizationMemberDAO(session),
        context_dao=context_dao,
    )


def seed_coordinator_transcript(
    session: Session,
    *,
    coordinator: Assistant,
    content: str,
    source_assistant_id: str | None,
) -> int:
    """Ensure the opener transcript contains one assistant row."""
    _lock_coordinator_context(
        session,
        coordinator=coordinator,
        suffix=COORDINATOR_TRANSCRIPTS_CONTEXT,
    )
    project = _project_for_coordinator(session, coordinator)
    context = _ensure_context(
        session,
        project_id=project.id,
        context_name=_coordinator_context_name(
            coordinator,
            COORDINATOR_TRANSCRIPTS_CONTEXT,
        ),
    )
    existing_id = session.scalar(
        select(LogEvent.id)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data["role"].astext == "assistant",
        )
        .order_by(LogEvent.id.desc())
        .limit(1),
    )
    if existing_id is not None:
        return existing_id

    context_dao = ContextDAO(session)
    result = create_logs_internal(
        request=CreateLogConfig(
            project_name=ASSISTANTS_PROJECT_NAME,
            context=_coordinator_context_name(
                coordinator,
                COORDINATOR_TRANSCRIPTS_CONTEXT,
            ),
            entries={
                "role": "assistant",
                "content": content,
                "assistant_id": source_assistant_id or str(coordinator.agent_id),
            },
        ),
        project_id=project.id,
        context_id=context.id,
        project_dao=_build_project_dao(session),
        field_type_dao=FieldTypeDAO(session),
        log_event_dao=LogEventDAO(session),
        context_dao=context_dao,
        context_obj=context,
    )
    session.flush()
    return result["log_event_ids"][0]


def reset_coordinator_state(session: Session, *, coordinator: Assistant) -> None:
    """Delete Coordinator-owned state contexts using the standard cleanup path."""
    project = _project_for_coordinator(session, coordinator)
    context_dao = ContextDAO(session)
    for context_name in COORDINATOR_RESET_CONTEXTS:
        context = _get_context(
            session,
            project_id=project.id,
            context_name=_coordinator_context_name(coordinator, context_name),
        )
        if context is not None:
            context_dao.delete(context.id, skip_embedding_cleanup=True)


def update_coordinator_mode(
    session: Session,
    *,
    coordinator: Assistant,
    mode: CoordinatorMode,
) -> CoordinatorMode:
    """Read-modify-write the single Coordinator onboarding mode row."""
    _lock_coordinator_context(
        session,
        coordinator=coordinator,
        suffix=COORDINATOR_STATE_CONTEXT,
    )
    project = _project_for_coordinator(session, coordinator)
    context = _ensure_context(
        session,
        project_id=project.id,
        context_name=_coordinator_context_name(coordinator, COORDINATOR_STATE_CONTEXT),
    )
    log_event = session.scalar(
        select(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .where(LogEventContext.context_id == context.id)
        .order_by(LogEvent.id.asc())
        .limit(1),
    )
    if log_event is None:
        result = create_logs_internal(
            request=CreateLogConfig(
                project_name=ASSISTANTS_PROJECT_NAME,
                context=_coordinator_context_name(
                    coordinator,
                    COORDINATOR_STATE_CONTEXT,
                ),
                entries={"mode": mode},
            ),
            project_id=project.id,
            context_id=context.id,
            project_dao=_build_project_dao(session),
            field_type_dao=FieldTypeDAO(session),
            log_event_dao=LogEventDAO(session),
            context_dao=ContextDAO(session),
            context_obj=context,
        )
        log_event = session.get(LogEvent, result["log_event_ids"][0])
    else:
        data = dict(log_event.data or {})
        data["mode"] = mode
        log_event.data = data

    session.flush()
    return mode
