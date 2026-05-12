"""Coordinator provisioning and lifecycle helpers."""

from datetime import datetime, timezone
from typing import Any, Sequence

from fastapi import HTTPException, status
from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session, aliased

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
    Context,
    LogEvent,
    LogEventContext,
    Project,
    User,
)
from orchestra.services.assistant_bootstrap import ensure_owner_contact_row
from orchestra.services.contact_membership_service import (
    PERSONAL_BOSS_CONTACT_ID,
    PERSONAL_SELF_CONTACT_ID,
    ensure_personal_contact_memberships,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    get_task_ids_for_log_ids,
    is_task_surface_context_name,
    sync_task_activations_for_task_ids,
)
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal
from orchestra.web.api.utils.assistant_infra import create_pubsub_topic

ASSISTANTS_PROJECT_NAME = "Assistants"
COORDINATOR_CONTEXT_PREFIX = "Coordinator"
COORDINATOR_DEFAULT_NATIONALITY = "United States"
COORDINATOR_RESET_CONTEXTS = (
    "Coordinator/State",
    "Coordinator/Checklist",
    "Transcripts",
    "Exchanges",
)
COORDINATOR_TRANSCRIPTS_CONTEXT = "Transcripts"
COORDINATOR_EXCHANGES_CONTEXT = "Exchanges"
COORDINATOR_CHAT_MEDIUM = "unify_message"
COORDINATOR_OPENER_SOURCE = "coordinator_opener"
PRESEED_SHARED_CONTEXT_PREFIX = "Spaces"
PRESEED_SERVER_FIELDS = frozenset(
    {"_user_id", "_assistant_id", "authoring_assistant_id"},
)
PRESEED_TASK_SERVER_FIELDS = frozenset({"assistant_id"})
TRANSCRIPTS_UNIQUE_KEYS = {"message_id": "int"}
EXCHANGES_UNIQUE_KEYS = {"exchange_id": "int"}
TRANSCRIPTS_AUTO_COUNTING = {"message_id": None}
EXCHANGES_AUTO_COUNTING = {"exchange_id": None}


def _ensure_coordinator_default_nationality(assistant: Assistant) -> None:
    """Ensure Coordinator rows carry the nationality required for runtime startup."""
    if assistant.nationality is None:
        assistant.nationality = COORDINATOR_DEFAULT_NATIONALITY


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
        nationality=COORDINATOR_DEFAULT_NATIONALITY,
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


def create_personal_coordinator(
    session: Session,
    user_id: str,
) -> tuple[Assistant, bool]:
    """Create or return the user's personal Coordinator.

    Returns ``(assistant, created)`` where ``created`` is ``True`` only when this
    call inserted the assistant row.
    """
    existing = get_personal_coordinator(session, user_id)
    if existing is not None:
        _ensure_coordinator_default_nationality(existing)
        ensure_personal_contact_memberships(session, [existing.agent_id])
        _ensure_coordinator_owner_contact_row(session, coordinator=existing)
        return existing, False

    assistant = create_coordinator_assistant(
        session,
        owner_user_id=user_id,
        organization_id=None,
    )
    ensure_personal_contact_memberships(
        session,
        [assistant.agent_id],
        repair_existing=False,
    )
    ensure_assistants_project(
        session,
        owner_user_id=user_id,
        organization_id=None,
    )
    _ensure_coordinator_owner_contact_row(session, coordinator=assistant)
    return assistant, True


async def ensure_personal_coordinator_provisioned(
    session: Session,
    *,
    user_id: str,
) -> tuple[Assistant, bool]:
    """Ensure personal Coordinator row and pubsub topic both exist.

    Returns ``(assistant, created)`` where ``created`` indicates whether this
    call created the Coordinator row.
    """
    coordinator, created_coordinator = create_personal_coordinator(session, user_id)
    pubsub_response = await create_pubsub_topic(
        str(coordinator.agent_id),
        deploy_env=coordinator.deploy_env,
    )
    if pubsub_topic_response_failed(pubsub_response):
        raise ValueError(f"Coordinator topic provisioning failed: {pubsub_response}")
    return coordinator, created_coordinator


def list_user_ids_missing_personal_coordinator(
    session: Session,
    *,
    limit: int | None = None,
) -> list[str]:
    """Return user IDs that do not yet have a personal Coordinator."""
    personal_coordinator = aliased(Assistant)
    stmt = (
        select(User.id)
        .outerjoin(
            personal_coordinator,
            and_(
                personal_coordinator.user_id == User.id,
                personal_coordinator.organization_id.is_(None),
                personal_coordinator.is_coordinator.is_(True),
            ),
        )
        .where(personal_coordinator.agent_id.is_(None))
        .order_by(User.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def create_organization_coordinator(
    session: Session,
    *,
    owner_user_id: str,
    organization_id: int,
    timezone: str | None,
) -> Assistant:
    """Create or return the organization's Coordinator."""
    existing = get_org_coordinator(session, organization_id)
    if existing is not None:
        _ensure_coordinator_default_nationality(existing)
        ensure_personal_contact_memberships(session, [existing.agent_id])
        _ensure_coordinator_owner_contact_row(session, coordinator=existing)
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
    ensure_personal_contact_memberships(
        session,
        [assistant.agent_id],
        repair_existing=False,
    )
    ensure_assistants_project(
        session,
        owner_user_id=owner_user_id,
        organization_id=organization_id,
    )
    _ensure_coordinator_owner_contact_row(session, coordinator=assistant)
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
    return coordinator


def require_authorized_preseed_target(
    session: Session,
    *,
    target_assistant_id: int,
    user_id: str,
) -> tuple[Assistant, Assistant]:
    """Resolve the Coordinator allowed to seed rows for one colleague."""
    target = AssistantDAO(session).get_assistant_by_agent_id(
        agent_id=target_assistant_id,
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    if target.is_coordinator:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot_preseed_coordinator",
        )

    if target.organization_id is None:
        if target.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to seed this assistant.",
            )
        coordinator = get_personal_coordinator(session, user_id)
    else:
        coordinator = get_org_coordinator(session, target.organization_id)

    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Coordinator not found.",
        )
    authorized = require_authorized_coordinator(
        session,
        coordinator_id=coordinator.agent_id,
        user_id=user_id,
    )
    if authorized.organization_id != target.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Coordinator cannot seed this assistant.",
        )
    return authorized, target


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
    unique_keys: dict[str, str] | None = None,
    auto_counting: dict[str, str | None] | None = None,
    allow_duplicates: bool | None = None,
) -> Context:
    context = _get_context(
        session,
        project_id=project_id,
        context_name=context_name,
    )
    if context is None:
        unique_keys = unique_keys or {}
        context = Context(
            project_id=project_id,
            name=context_name,
            is_versioned=False,
            allow_duplicates=True if allow_duplicates is None else allow_duplicates,
            unique_key_names=list(unique_keys.keys()),
            unique_key_types=list(unique_keys.values()),
            auto_counting=auto_counting or {},
        )
        session.add(context)
        session.flush()
    else:
        if unique_keys and not context.unique_keys:
            context.unique_key_names = list(unique_keys.keys())
            context.unique_key_types = list(unique_keys.values())
        if auto_counting and not context.auto_counting:
            context.auto_counting = auto_counting
        if (
            allow_duplicates is not None
            and context.allow_duplicates != allow_duplicates
        ):
            context.allow_duplicates = allow_duplicates
    return context


def _create_coordinator_log_entry(
    session: Session,
    *,
    project: Project,
    context: Context,
    context_name: str,
    entries: dict[str, Any],
) -> dict[str, Any]:
    context_dao = ContextDAO(session)
    result = create_logs_internal(
        request=CreateLogConfig(
            project_name=ASSISTANTS_PROJECT_NAME,
            context=context_name,
            entries=entries,
        ),
        project_id=project.id,
        context_id=context.id,
        project_dao=_build_project_dao(session),
        field_type_dao=FieldTypeDAO(session),
        log_event_dao=LogEventDAO(session),
        context_dao=context_dao,
        context_obj=context,
    )
    if result.get("failed"):
        first_error = result["failed"][0].get("error", "Log creation failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=first_error,
        )
    return result


def _context_has_logs(
    session: Session,
    *,
    context: Context,
) -> bool:
    return (
        session.scalar(
            select(LogEvent.id)
            .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
            .where(
                LogEventContext.context_id == context.id,
            )
            .limit(1),
        )
        is not None
    )


def _ensure_coordinator_owner_contact_row(
    session: Session,
    *,
    coordinator: Assistant,
) -> int:
    """Ensure the owner can be resolved as the Coordinator's chat contact."""
    return ensure_owner_contact_row(
        session,
        assistant=coordinator,
        project=_project_for_coordinator(session, coordinator),
    )


def ensure_coordinator_owner_contact_rows(
    session: Session,
    assistant_ids: Sequence[int],
) -> None:
    """Ensure listed Coordinator assistants have owner contact rows for chat."""
    if not assistant_ids:
        return
    coordinators = session.scalars(
        select(Assistant).where(
            Assistant.agent_id.in_(assistant_ids),
            Assistant.is_coordinator.is_(True),
        ),
    ).all()
    for coordinator in coordinators:
        _ensure_coordinator_owner_contact_row(session, coordinator=coordinator)
    session.flush()


def _build_project_dao(session: Session) -> ProjectDAO:
    context_dao = ContextDAO(session)
    return ProjectDAO(
        session,
        organization_member_dao=OrganizationMemberDAO(session),
        context_dao=context_dao,
    )


def _normalize_preseed_context(context_name: str) -> str:
    """Return a relative context suffix that remains under a target assistant root."""
    normalized = (context_name or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preseed context must be a non-empty relative path.",
        )
    if normalized.startswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preseed context must be relative.",
        )
    segments = normalized.strip("/").split("/")
    if (
        not segments
        or any(segment in {"", ".", ".."} for segment in segments)
        or segments[0] == PRESEED_SHARED_CONTEXT_PREFIX
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preseed context must stay inside the target assistant root.",
        )
    return "/".join(segments)


def _preseed_context_name(*, target: Assistant, context_suffix: str) -> str:
    return f"{target.user_id}/{target.agent_id}/{context_suffix}"


def _preseed_protected_fields(*, is_task_context: bool) -> set[str]:
    protected_fields = set(PRESEED_SERVER_FIELDS)
    if is_task_context:
        protected_fields.update(PRESEED_TASK_SERVER_FIELDS)
    return protected_fields


def _ensure_preseed_entry_is_client_owned(
    entry: dict[str, Any],
    *,
    is_task_context: bool,
) -> None:
    blocked = sorted(
        _preseed_protected_fields(is_task_context=is_task_context) & set(entry),
    )
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Preseed entries cannot set server-owned fields: {blocked}.",
        )


def _preseed_entry(
    entry: dict[str, Any],
    *,
    target: Assistant,
    coordinator: Assistant,
    is_task_context: bool,
) -> dict[str, Any]:
    seeded = dict(entry)
    seeded["authoring_assistant_id"] = coordinator.agent_id
    if is_task_context:
        seeded["_user_id"] = target.user_id
        seeded["_assistant_id"] = str(target.agent_id)
    return seeded


def _validate_preseed_writes(
    writes: Sequence[Any],
) -> list[tuple[str, bool, list[dict[str, Any]]]]:
    """Validate requested writes before any row is persisted."""
    planned_writes: list[tuple[str, bool, list[dict[str, Any]]]] = []
    for write in writes:
        context_suffix = _normalize_preseed_context(write.context)
        entries = list(write.entries)
        if not entries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Preseed writes must include at least one entry.",
            )
        is_task_context = is_task_surface_context_name(context_suffix)
        for entry in entries:
            _ensure_preseed_entry_is_client_owned(
                entry,
                is_task_context=is_task_context,
            )
        planned_writes.append((context_suffix, is_task_context, entries))
    return planned_writes


def preseed_colleague_contexts(
    session: Session,
    *,
    coordinator: Assistant,
    target: Assistant,
    writes: Sequence[Any],
) -> list[dict[str, Any]]:
    """Write Coordinator-authored rows into one colleague's own contexts."""
    planned_writes = _validate_preseed_writes(writes)
    project = ensure_assistants_project(
        session,
        owner_user_id=target.user_id,
        organization_id=target.organization_id,
    )
    if project.name != TASK_MACHINE_PROJECT_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unsupported assistant project.",
        )

    context_dao = ContextDAO(session)
    project_dao = _build_project_dao(session)
    field_type_dao = FieldTypeDAO(session)
    log_event_dao = LogEventDAO(session)

    results: list[dict[str, Any]] = []
    for context_suffix, is_task_context, entries in planned_writes:
        context_name = _preseed_context_name(
            target=target,
            context_suffix=context_suffix,
        )
        seeded_entries = [
            _preseed_entry(
                entry,
                target=target,
                coordinator=coordinator,
                is_task_context=is_task_context,
            )
            for entry in entries
        ]
        context = _ensure_context(
            session,
            project_id=project.id,
            context_name=context_name,
        )
        result = create_logs_internal(
            request=CreateLogConfig(
                project_name=project.name,
                context=context_name,
                entries=seeded_entries,
            ),
            project_id=project.id,
            context_id=context.id,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            log_event_dao=log_event_dao,
            context_dao=context_dao,
            context_obj=context,
        )
        if result.get("failed"):
            first_error = result["failed"][0].get("error", "Log creation failed")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=first_error,
            )

        log_event_ids = result["log_event_ids"]
        if is_task_context and log_event_ids:
            task_ids = get_task_ids_for_log_ids(
                session=session,
                project_id=project.id,
                context_name=context_name,
                log_event_ids=log_event_ids,
            )
            sync_task_activations_for_task_ids(
                session=session,
                project_id=project.id,
                task_ids=task_ids,
                tasks_context_name=context_name,
            )

        results.append(
            {
                "context": context_name,
                "log_event_ids": log_event_ids,
                "row_ids": result["row_ids"],
                "auto_counting": result["auto_counting"],
            },
        )

    session.flush()
    return results


def seed_coordinator_transcript(
    session: Session,
    *,
    coordinator: Assistant,
    content: str,
    source_assistant_id: str | None,
) -> int:
    """Ensure the opener transcript contains one visible chat row."""
    _lock_coordinator_context(
        session,
        coordinator=coordinator,
        suffix=COORDINATOR_TRANSCRIPTS_CONTEXT,
    )
    _ensure_coordinator_owner_contact_row(session, coordinator=coordinator)
    project = _project_for_coordinator(session, coordinator)
    transcript_context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_TRANSCRIPTS_CONTEXT,
    )
    transcript_context = _ensure_context(
        session,
        project_id=project.id,
        context_name=transcript_context_name,
        unique_keys=TRANSCRIPTS_UNIQUE_KEYS,
        auto_counting=TRANSCRIPTS_AUTO_COUNTING,
    )
    exchange_context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_EXCHANGES_CONTEXT,
    )
    exchange_context = _ensure_context(
        session,
        project_id=project.id,
        context_name=exchange_context_name,
        unique_keys=EXCHANGES_UNIQUE_KEYS,
        auto_counting=EXCHANGES_AUTO_COUNTING,
    )
    if _context_has_logs(
        session,
        context=transcript_context,
    ) or _context_has_logs(
        session,
        context=exchange_context,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="coordinator_transcript_not_empty",
        )
    exchange_result = _create_coordinator_log_entry(
        session,
        project=project,
        context=exchange_context,
        context_name=exchange_context_name,
        entries={
            "medium": COORDINATOR_CHAT_MEDIUM,
            "metadata": {"source": COORDINATOR_OPENER_SOURCE},
        },
    )
    exchange_id = exchange_result["auto_counting"]["exchange_id"][0]
    transcript_result = _create_coordinator_log_entry(
        session,
        project=project,
        context=transcript_context,
        context_name=transcript_context_name,
        entries={
            "medium": COORDINATOR_CHAT_MEDIUM,
            "sender_id": PERSONAL_SELF_CONTACT_ID,
            "receiver_ids": [PERSONAL_BOSS_CONTACT_ID],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": content,
            "exchange_id": exchange_id,
            "images": [],
            "attachments": [],
            "metadata": {
                "source": COORDINATOR_OPENER_SOURCE,
                "source_assistant_id": source_assistant_id or str(coordinator.agent_id),
            },
        },
    )
    session.flush()
    return transcript_result["log_event_ids"][0]


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
