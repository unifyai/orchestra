"""Coordinator provisioning and lifecycle helpers."""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx
from fastapi import HTTPException, status
from sqlalchemy import Integer, and_, literal, select, text
from sqlalchemy.orm import Session, aliased

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.assistant_secret_dao import AssistantSecretDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.db.dao.slack_dao import SlackDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.log_queries import project_scoped_log_events
from orchestra.db.models.coordinator_voice import (
    COORDINATOR_DEFAULT_VOICE_ID,
    COORDINATOR_DEFAULT_VOICE_PROVIDER,
    ensure_coordinator_voice_row,
)
from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Organization,
    OrganizationMember,
    Project,
    User,
)
from orchestra.db.scope import single_owner_key
from orchestra.services import onboarding_graph
from orchestra.services.assistant_bootstrap import ensure_owner_contact_row
from orchestra.services.contact_membership_service import (
    PERSONAL_BOSS_CONTACT_ID,
    PERSONAL_SELF_CONTACT_ID,
    ensure_personal_contact_memberships,
)
from orchestra.services.universal_unity_discord import (
    ensure_coordinator_universal_discord_contact,
)
from orchestra.services.universal_unity_email import (
    ensure_coordinator_universal_email_contact,
)
from orchestra.services.universal_unity_phone import (
    ensure_coordinator_universal_phone_contact,
)
from orchestra.services.universal_unity_whatsapp import (
    ensure_coordinator_universal_whatsapp_contact,
)
from orchestra.settings import settings
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal
from orchestra.web.api.utils.assistant_infra import (
    ADMIN_KEY,
    _adapters_url,
    _post_unity_system_event,
    create_pubsub_topic,
)

logger = logging.getLogger(__name__)

ASSISTANTS_PROJECT_NAME = "Assistants"
COORDINATOR_CONTEXT_PREFIX = "Coordinator"
COORDINATOR_DEFAULT_NATIONALITY = "United States"
COORDINATOR_DEFAULT_DESKTOP_MODE = "ubuntu"
COORDINATOR_DEFAULT_FIRST_NAME = "T-W1N"
COORDINATOR_DEFAULT_JOB_TITLE = "Coordinator"
COORDINATOR_STATE_CONTEXT = "Coordinator/State"
COORDINATOR_RESET_CONTEXTS = (
    COORDINATOR_STATE_CONTEXT,
    "Coordinator/Checklist",
    "Transcripts",
    "Exchanges",
)
COORDINATOR_TRANSCRIPTS_CONTEXT = "Transcripts"
COORDINATOR_EXCHANGES_CONTEXT = "Exchanges"
COORDINATOR_CHAT_MEDIUM = "unify_message"
COORDINATOR_OPENER_SOURCE = "coordinator_opener"

# Coordinator onboarding gate on the ``Coordinator/State`` context: a
# freshly-provisioned Coordinator starts with ``onboarding_active=True``
# and the assistants surface renders the guided call-or-chat view until
# onboarding is paused or finished (``onboarding_active=False``).
TRANSCRIPTS_UNIQUE_KEYS = {"message_id": "int"}
TRANSCRIPTS_UNIQUE_KEYS = {"message_id": "int"}
EXCHANGES_UNIQUE_KEYS = {"exchange_id": "int"}
TRANSCRIPTS_AUTO_COUNTING = {"message_id": None}
EXCHANGES_AUTO_COUNTING = {"exchange_id": None}


def _ensure_coordinator_default_nationality(assistant: Assistant) -> None:
    """Ensure Coordinator rows carry the nationality required for runtime startup."""
    if assistant.nationality is None:
        assistant.nationality = COORDINATOR_DEFAULT_NATIONALITY


def _ensure_coordinator_default_desktop_mode(assistant: Assistant) -> None:
    """Ensure Coordinator rows request a managed desktop when unset."""
    if not assistant.desktop_mode:
        assistant.desktop_mode = COORDINATOR_DEFAULT_DESKTOP_MODE


def get_workspace_coordinator(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
) -> Assistant | None:
    """Return the Coordinator row for one workspace scope when it exists."""
    stmt = select(Assistant).where(
        Assistant.user_id == user_id,
        Assistant.is_coordinator.is_(True),
    )
    if organization_id is None:
        stmt = stmt.where(Assistant.organization_id.is_(None))
    else:
        stmt = stmt.where(Assistant.organization_id == organization_id)
    return session.scalar(stmt)


def get_personal_coordinator(session: Session, user_id: str) -> Assistant | None:
    """Return the user's personal Coordinator when one already exists."""
    return get_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=None,
    )


def get_organization_coordinator(
    session: Session,
    *,
    user_id: str,
    organization_id: int,
) -> Assistant | None:
    """Return the user's organization-scoped Coordinator when one exists."""
    return session.scalar(
        select(Assistant).where(
            Assistant.user_id == user_id,
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
    """Create a Coordinator assistant row for one workspace scope."""
    ensure_coordinator_voice_row(session.connection(), owner_user_id)
    assistant = AssistantDAO(session).create_assistant(
        user_id=owner_user_id,
        first_name=COORDINATOR_DEFAULT_FIRST_NAME,
        surname=None,
        age=None,
        nationality=COORDINATOR_DEFAULT_NATIONALITY,
        profile_photo=None,
        profile_video=None,
        desktop_mode=COORDINATOR_DEFAULT_DESKTOP_MODE,
        about="",
        weekly_limit=None,
        max_parallel=None,
        voice_id=COORDINATOR_DEFAULT_VOICE_ID,
        voice_provider=COORDINATOR_DEFAULT_VOICE_PROVIDER,
        timezone=timezone,
        organization_id=organization_id,
        is_local=False,
        is_coordinator=True,
        job_title=COORDINATOR_DEFAULT_JOB_TITLE,
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
        organization_owner_user_id = session.scalar(
            select(Organization.owner_id).where(Organization.id == organization_id),
        )
        if organization_owner_user_id is None:
            raise ValueError(f"Organization {organization_id} not found")
        grant_project_access_to_org_members(
            session,
            project=project,
            owner_user_id=organization_owner_user_id,
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


def _repair_existing_coordinator_state(
    session: Session,
    *,
    coordinator: Assistant,
    preferred_phone_country: str | None = None,
    intro_watched: bool = False,
) -> None:
    """Repair Coordinator defaults and required owner-facing overlays."""
    _ensure_coordinator_default_nationality(coordinator)
    _ensure_coordinator_default_desktop_mode(coordinator)
    ensure_personal_contact_memberships(session, [coordinator.agent_id])
    _ensure_coordinator_owner_contact_row(session, coordinator=coordinator)
    ensure_coordinator_universal_email_contact(session, coordinator=coordinator)
    ensure_coordinator_universal_whatsapp_contact(session, coordinator=coordinator)
    ensure_coordinator_universal_discord_contact(session, coordinator=coordinator)
    ensure_coordinator_universal_phone_contact(
        session,
        coordinator=coordinator,
        preferred_country=preferred_phone_country,
        assignment_source="geo" if preferred_phone_country else "repair",
    )
    # Backfill the state row for Coordinators provisioned before the
    # onboarding-mode flow shipped. New rows arrive via the create path
    # below; this branch picks up the long tail of pre-existing
    # Coordinators on their next visit. Idempotent — no-op when a state
    # row already exists.
    seed_initial_coordinator_state(
        session,
        coordinator=coordinator,
        intro_watched=intro_watched,
    )
    if intro_watched:
        ensure_coordinator_intro_watched(session, coordinator=coordinator)


def heal_coordinator_universal_contacts(
    session: Session,
    *,
    coordinator: Assistant,
    contact_types: Sequence[str],
) -> None:
    """Provision or reconcile the named universal contacts on a Coordinator.

    A focused, idempotent subset of :func:`_repair_existing_coordinator_state`
    used by the read path to (a) backfill Coordinators that predate the
    universal-contact rollout (or a newly added channel) and (b) reconcile
    contacts whose stored value has drifted from the value currently
    configured for this deployment. Only the channels in ``contact_types`` are
    touched; each ``ensure_*`` call is idempotent — it re-points the contact at
    the configured value when it differs and is a no-op when it already
    matches.

    The phone backfill uses the platform default country rather than the
    visitor's geo (the read request doesn't carry it); new Coordinators still
    get geo-aware phone selection through the onboarding provisioning path.
    """
    if not coordinator.is_coordinator:
        return

    requested = set(contact_types)
    if "email" in requested:
        ensure_coordinator_universal_email_contact(session, coordinator=coordinator)
    if "whatsapp" in requested:
        ensure_coordinator_universal_whatsapp_contact(session, coordinator=coordinator)
    if "discord" in requested:
        ensure_coordinator_universal_discord_contact(session, coordinator=coordinator)
    if "phone" in requested:
        ensure_coordinator_universal_phone_contact(
            session,
            coordinator=coordinator,
            assignment_source="repair",
        )


def create_workspace_coordinator(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    preferred_phone_country: str | None = None,
    initial_intro_watched: bool = False,
) -> tuple[Assistant, bool]:
    """Create or return the user's Coordinator for one workspace.

    Returns ``(assistant, created)`` where ``created`` is ``True`` only when this
    call inserted the assistant row.
    """
    existing = get_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=organization_id,
    )
    if existing is not None:
        _repair_existing_coordinator_state(
            session,
            coordinator=existing,
            preferred_phone_country=preferred_phone_country,
            intro_watched=initial_intro_watched,
        )
        return existing, False

    assistant = create_coordinator_assistant(
        session,
        owner_user_id=user_id,
        organization_id=organization_id,
    )
    ensure_personal_contact_memberships(
        session,
        [assistant.agent_id],
        repair_existing=False,
    )
    ensure_assistants_project(
        session,
        owner_user_id=user_id,
        organization_id=organization_id,
    )
    _ensure_coordinator_owner_contact_row(session, coordinator=assistant)
    ensure_coordinator_universal_email_contact(session, coordinator=assistant)
    ensure_coordinator_universal_whatsapp_contact(session, coordinator=assistant)
    ensure_coordinator_universal_discord_contact(session, coordinator=assistant)
    ensure_coordinator_universal_phone_contact(
        session,
        coordinator=assistant,
        preferred_country=preferred_phone_country,
        assignment_source="geo" if preferred_phone_country else "auto",
    )
    # Seed the starting Coordinator/State row so the assistants page can
    # decide between the onboarding view and the regular view from a
    # single read. Freshly-created Coordinators land in
    # ``onboarding`` mode with no picker choice made yet.
    seed_initial_coordinator_state(
        session,
        coordinator=assistant,
        intro_watched=initial_intro_watched,
    )
    return assistant, True


def create_personal_coordinator(
    session: Session,
    user_id: str,
    preferred_phone_country: str | None = None,
    initial_intro_watched: bool = False,
) -> tuple[Assistant, bool]:
    """Create or return the user's personal Coordinator."""
    return create_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=None,
        preferred_phone_country=preferred_phone_country,
        initial_intro_watched=initial_intro_watched,
    )


async def ensure_workspace_coordinator_provisioned(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    preferred_phone_country: str | None = None,
    initial_intro_watched: bool = False,
) -> tuple[Assistant, bool]:
    """Ensure workspace Coordinator row and pubsub topic both exist.

    Returns ``(assistant, created)`` where ``created`` indicates whether this
    call created the Coordinator row.
    """
    coordinator, created_coordinator = create_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=organization_id,
        preferred_phone_country=preferred_phone_country,
        initial_intro_watched=initial_intro_watched,
    )
    pubsub_response = await create_pubsub_topic(
        str(coordinator.agent_id),
    )
    if pubsub_topic_response_failed(pubsub_response):
        raise ValueError(f"Coordinator topic provisioning failed: {pubsub_response}")
    return coordinator, created_coordinator


async def ensure_personal_coordinator_provisioned(
    session: Session,
    *,
    user_id: str,
    preferred_phone_country: str | None = None,
    initial_intro_watched: bool = False,
) -> tuple[Assistant, bool]:
    """Ensure personal Coordinator row and pubsub topic both exist."""
    return await ensure_workspace_coordinator_provisioned(
        session,
        user_id=user_id,
        organization_id=None,
        preferred_phone_country=preferred_phone_country,
        initial_intro_watched=initial_intro_watched,
    )


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


def list_workspace_memberships_missing_coordinator(
    session: Session,
    *,
    limit: int | None = None,
) -> list[tuple[str, int | None]]:
    """Return workspace memberships that do not yet have a Coordinator."""
    personal_coordinator = aliased(Assistant)
    personal_targets = (
        select(
            User.id.label("user_id"),
            literal(None, type_=Integer).label("organization_id"),
            User.created_at.label("created_at"),
        )
        .outerjoin(
            personal_coordinator,
            and_(
                personal_coordinator.user_id == User.id,
                personal_coordinator.organization_id.is_(None),
                personal_coordinator.is_coordinator.is_(True),
            ),
        )
        .where(personal_coordinator.agent_id.is_(None))
    )

    org_coordinator = aliased(Assistant)
    org_targets = (
        select(
            OrganizationMember.user_id.label("user_id"),
            OrganizationMember.organization_id.label("organization_id"),
            OrganizationMember.created_at.label("created_at"),
        )
        .outerjoin(
            org_coordinator,
            and_(
                org_coordinator.user_id == OrganizationMember.user_id,
                org_coordinator.organization_id == OrganizationMember.organization_id,
                org_coordinator.is_coordinator.is_(True),
            ),
        )
        .where(org_coordinator.agent_id.is_(None))
    )

    stmt = personal_targets.union_all(org_targets).order_by(text("created_at ASC"))
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    return [(row.user_id, row.organization_id) for row in rows]


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
    if coordinator.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this Coordinator.",
        )
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
    return coordinator


def require_authorized_delegate_target(
    session: Session,
    *,
    target_assistant_id: int,
    user_id: str,
) -> tuple[Assistant, Assistant]:
    """Resolve the workspace Coordinator allowed to delegate work to one colleague."""
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
            detail="cannot_delegate_to_coordinator",
        )

    resource_access_dao = ResourceAccessDAO(session)
    if target.organization_id is None:
        if target.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delegate to this assistant.",
            )
    else:
        has_target_write = resource_access_dao.check_user_permission(
            user_id,
            "assistant",
            target.agent_id,
            "assistant:write",
        ) or resource_access_dao.check_org_member_permission(
            user_id,
            target.organization_id,
            "assistant:write",
        )
        if not has_target_write:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delegate to this assistant.",
            )

    coordinator = get_workspace_coordinator(
        session,
        user_id=user_id,
        organization_id=target.organization_id,
    )

    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace Coordinator not found.",
        )
    authorized = require_authorized_coordinator(
        session,
        coordinator_id=coordinator.agent_id,
        user_id=user_id,
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
            project_scoped_log_events(
                context.project_id,
                LogEvent.id,
                owner_key=single_owner_key(context.owner_scope, context.owner_id),
            )
            .where(LogEventContext.context_id == context.id)
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


# ─── Coordinator/State (onboarding mode + step) ─────────────────────────────


def _latest_coordinator_state_row(
    session: Session,
    *,
    project: Project,
    state_context_name: str,
) -> dict[str, Any] | None:
    """Return the most recent ``Coordinator/State`` row, if any.

    The state context is append-only — every transition inserts a new
    log row. The frontend reads with ``limit: 1`` ordered by
    ``timestamp`` descending, so we mirror the same selection here
    when computing the "current" state for merges.
    """
    context = _get_context(
        session,
        project_id=project.id,
        context_name=state_context_name,
    )
    if context is None:
        return None
    log = session.scalar(
        project_scoped_log_events(
            project.id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(LogEventContext.context_id == context.id)
        .order_by(LogEvent.id.desc())
        .limit(1),
    )
    if log is None:
        return None
    data = log.data or {}
    if isinstance(data, dict):
        return dict(data)
    return None


def _coordinator_state_entry(
    *,
    onboarding_active: bool,
    onboarding_step: str | None,
    skipped_step_ids: Sequence[str],
    skipped_phase_ids: Sequence[str],
    manually_completed_step_ids: Sequence[str],
    onboarding_reset_at: dict[str, str] | None,
    previous: dict[str, Any] | None,
    intro_watched: bool | None = None,
) -> dict[str, Any]:
    """Build a fully-formed ``Coordinator/State`` row.

    ``started_at`` is sticky across transitions (captured the first
    time we ever write a row, regardless of onboarding activity, so we
    always know when the lifecycle began).

    ``ended_at`` tracks the current inactive stretch: it is stamped when
    ``onboarding_active`` becomes ``False`` (and stays put while
    inactive), but cleared when onboarding resumes — otherwise an active
    row would carry a stale "ended" timestamp. A subsequent deactivation
    re-stamps ``ended_at`` to the new transition time.
    """
    now = datetime.now(timezone.utc).isoformat()
    started_at = (previous or {}).get("started_at") if previous else None
    if not started_at:
        started_at = now
    previous_ended_at = (previous or {}).get("ended_at") if previous else None
    if not onboarding_active:
        ended_at = previous_ended_at or now
    else:
        ended_at = None
    # ``intro_watched`` is one-way sticky: once the user has resolved the
    # opening picker we never want the ringing picker / auto-playing intro
    # to re-appear, so a later row cannot flip it back to ``False``.
    next_intro_watched = bool((previous or {}).get("intro_watched")) or bool(
        intro_watched,
    )
    return {
        "onboarding_active": onboarding_active,
        "onboarding_step": onboarding_step,
        "skipped_step_ids": list(skipped_step_ids),
        "skipped_phase_ids": list(skipped_phase_ids),
        "manually_completed_step_ids": list(manually_completed_step_ids),
        "onboarding_reset_at": dict(onboarding_reset_at or {}),
        "started_at": started_at,
        "ended_at": ended_at,
        "intro_watched": next_intro_watched,
        "timestamp": now,
    }


def _write_coordinator_state_row(
    session: Session,
    *,
    coordinator: Assistant,
    entry: dict[str, Any],
) -> int:
    """Persist one ``Coordinator/State`` row and return its log event id."""
    project = _project_for_coordinator(session, coordinator)
    context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_STATE_CONTEXT,
    )
    context = _ensure_context(
        session,
        project_id=project.id,
        context_name=context_name,
    )
    result = _create_coordinator_log_entry(
        session,
        project=project,
        context=context,
        context_name=context_name,
        entries=entry,
    )
    return result["log_event_ids"][0]


def get_coordinator_state(
    session: Session,
    *,
    coordinator: Assistant,
) -> dict[str, Any]:
    """Return the latest ``Coordinator/State`` row, normalised.

    Falls back to a synthetic active snapshot when no row has
    been written yet — the create/repair paths seed an initial row,
    but the endpoint stays well-behaved even if a Coordinator slipped
    through without one (e.g. inserted directly via seed scripts).
    """
    project = _project_for_coordinator(session, coordinator)
    state_context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_STATE_CONTEXT,
    )
    row = _latest_coordinator_state_row(
        session,
        project=project,
        state_context_name=state_context_name,
    )
    if row is None:
        return {
            "onboarding_active": True,
            "onboarding_step": None,
            "skipped_step_ids": [],
            "skipped_phase_ids": [],
            "manually_completed_step_ids": [],
            "onboarding_reset_at": {},
            "started_at": None,
            "ended_at": None,
            "intro_watched": False,
        }
    onboarding_step = row.get("onboarding_step")
    if onboarding_step is not None and not isinstance(onboarding_step, str):
        onboarding_step = None
    return {
        "onboarding_active": bool(row.get("onboarding_active", False)),
        "onboarding_step": onboarding_step,
        "skipped_step_ids": normalize_onboarding_step_ids(row.get("skipped_step_ids")),
        "skipped_phase_ids": normalize_onboarding_phase_ids(
            row.get("skipped_phase_ids"),
        ),
        "manually_completed_step_ids": normalize_onboarding_step_ids(
            row.get("manually_completed_step_ids"),
        ),
        "onboarding_reset_at": normalize_onboarding_reset_at(
            row.get("onboarding_reset_at"),
        ),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "intro_watched": bool(row.get("intro_watched", False)),
    }


def seed_initial_coordinator_state(
    session: Session,
    *,
    coordinator: Assistant,
    intro_watched: bool = False,
) -> int | None:
    """Ensure a freshly-provisioned Coordinator has a starting state row.

    Idempotent: a no-op when any state row already exists, so this is
    safe to call from both the create path and the repair path. Returns
    the new row's log event id when one is written, else ``None``.
    """
    _lock_coordinator_context(
        session,
        coordinator=coordinator,
        suffix=COORDINATOR_STATE_CONTEXT,
    )
    project = _project_for_coordinator(session, coordinator)
    state_context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_STATE_CONTEXT,
    )
    existing = _latest_coordinator_state_row(
        session,
        project=project,
        state_context_name=state_context_name,
    )
    if existing is not None:
        return None
    entry = _coordinator_state_entry(
        onboarding_active=True,
        onboarding_step=None,
        skipped_step_ids=[],
        skipped_phase_ids=[],
        manually_completed_step_ids=[],
        onboarding_reset_at={},
        previous=None,
        intro_watched=intro_watched,
    )
    log_event_id = _write_coordinator_state_row(
        session,
        coordinator=coordinator,
        entry=entry,
    )
    session.flush()
    return log_event_id


def set_coordinator_state(
    session: Session,
    *,
    coordinator: Assistant,
    onboarding_active: bool | None = None,
    onboarding_step: str | None = None,
    clear_onboarding_step: bool = False,
    skip_onboarding_step: str | None = None,
    unskip_onboarding_step: str | None = None,
    reset_onboarding_step: str | None = None,
    skip_onboarding_phase: str | None = None,
    unskip_onboarding_phase: str | None = None,
    intro_watched: bool | None = None,
    onboarding_step_completion: tuple[str, bool] | None = None,
    onboarding_reset_at_updates: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Append a new ``Coordinator/State`` row by merging with the latest.

    Only the fields explicitly supplied are touched — everything else
    is carried forward from the previous row so the frontend can rely
    on a single read returning the full picture.

    ``clear_onboarding_step=True`` resets the step back to ``None``
    (used when deactivating onboarding — the in-flight step no
    longer applies). Callers should not pass both ``onboarding_step``
    and ``clear_onboarding_step``; the explicit value wins if they do.

    The specific call-vs-chat surface choice is *not* persisted — only
    that the picker was resolved at all, via ``intro_watched``. Once
    that flag is set the ringing picker and auto-playing intro never
    re-appear on a later page load; the user replays the intro on
    demand from the onboarding pane instead.
    """
    if onboarding_step is not None and (
        not isinstance(onboarding_step, str)
        or not onboarding_step.strip()
        or onboarding_step not in SKIPPABLE_ONBOARDING_STEP_SET
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_onboarding_step",
        )
    if skip_onboarding_step is not None and (
        not isinstance(skip_onboarding_step, str)
        or skip_onboarding_step not in SKIPPABLE_ONBOARDING_STEPS
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_skip_onboarding_step",
        )
    if unskip_onboarding_step is not None and (
        not isinstance(unskip_onboarding_step, str)
        or unskip_onboarding_step not in SKIPPABLE_ONBOARDING_STEPS
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_unskip_onboarding_step",
        )
    if reset_onboarding_step is not None and (
        not isinstance(reset_onboarding_step, str)
        or reset_onboarding_step not in onboarding_graph.STEP_BY_ID
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_reset_onboarding_step",
        )
    if skip_onboarding_phase is not None and (
        not isinstance(skip_onboarding_phase, str)
        or skip_onboarding_phase not in SKIPPABLE_ONBOARDING_PHASES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_skip_onboarding_phase",
        )
    if unskip_onboarding_phase is not None and (
        not isinstance(unskip_onboarding_phase, str)
        or unskip_onboarding_phase not in SKIPPABLE_ONBOARDING_PHASES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_unskip_onboarding_phase",
        )
    if onboarding_step_completion is not None:
        step_id, _completed = onboarding_step_completion
        if (
            not isinstance(step_id, str)
            or not step_id.strip()
            or step_id not in onboarding_graph.STEP_BY_ID
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_onboarding_step_completion",
            )
    _lock_coordinator_context(
        session,
        coordinator=coordinator,
        suffix=COORDINATOR_STATE_CONTEXT,
    )
    project = _project_for_coordinator(session, coordinator)
    state_context_name = _coordinator_context_name(
        coordinator,
        COORDINATOR_STATE_CONTEXT,
    )
    previous = _latest_coordinator_state_row(
        session,
        project=project,
        state_context_name=state_context_name,
    )
    reset_at = normalize_onboarding_reset_at(
        (previous or {}).get("onboarding_reset_at"),
    )
    reset_step_ids: tuple[str, ...] = ()
    if reset_onboarding_step is not None:
        reset_step_ids = onboarding_graph.completion_coupled_steps(
            reset_onboarding_step,
        )
        reset_timestamp = datetime.now(timezone.utc).isoformat()
        for step_id in reset_step_ids:
            reset_at[step_id] = reset_timestamp
        user = session.get(User, coordinator.user_id)
        if user is not None:
            if ONBOARDING_STEP_WHATSAPP_NUMBER in reset_step_ids:
                user.whatsapp_number = None
            if ONBOARDING_STEP_PHONE_NUMBER in reset_step_ids:
                user.phone_number = None
    if onboarding_reset_at_updates:
        valid_step_ids = {step.id for step in onboarding_graph.ONBOARDING_GRAPH}
        for step_id, timestamp in onboarding_reset_at_updates.items():
            if (
                step_id in valid_step_ids
                and isinstance(timestamp, str)
                and _parse_onboarding_reset_at(timestamp) is not None
            ):
                reset_at[step_id] = timestamp
    next_onboarding_active = (
        bool(onboarding_active)
        if onboarding_active is not None
        else bool((previous or {}).get("onboarding_active", True))
    )
    if onboarding_step is not None:
        next_step: str | None = onboarding_step
    elif clear_onboarding_step:
        next_step = None
    else:
        next_step = (previous or {}).get("onboarding_step")
    next_skipped_step_ids = normalize_onboarding_step_ids(
        (previous or {}).get("skipped_step_ids"),
    )
    if skip_onboarding_step is not None:
        # Skips cascade only downward: skipping a step also skips the steps that
        # become unreachable without it (its completion-blocked descendants), but
        # never its prerequisites. Skipping "apps" must not skip "workspace".
        skipped_step_set = {
            skip_onboarding_step,
            *onboarding_graph.completion_blocked_descendants(skip_onboarding_step),
            *next_skipped_step_ids,
        }
        next_skipped_step_ids = [
            step_id
            for step_id in SKIPPABLE_ONBOARDING_STEPS
            if step_id in skipped_step_set
        ]
    if unskip_onboarding_step is not None:
        # Unskip mirrors skip: it re-offers the step and the descendants that were
        # only skipped because they depended on it, leaving prerequisites untouched.
        unskipped_step_set = {
            unskip_onboarding_step,
            *onboarding_graph.completion_blocked_descendants(unskip_onboarding_step),
        }
        next_skipped_step_ids = [
            step_id
            for step_id in next_skipped_step_ids
            if step_id not in unskipped_step_set
        ]
    if reset_step_ids:
        reset_step_set = set(reset_step_ids)
        next_skipped_step_ids = [
            step_id
            for step_id in next_skipped_step_ids
            if step_id not in reset_step_set
        ]
    next_manually_completed_step_ids = normalize_onboarding_step_ids(
        (previous or {}).get("manually_completed_step_ids"),
    )
    if reset_step_ids:
        reset_step_set = set(reset_step_ids)
        next_manually_completed_step_ids = [
            step_id
            for step_id in next_manually_completed_step_ids
            if step_id not in reset_step_set
        ]
    if onboarding_step_completion is not None:
        completion_step_id, completion_completed = onboarding_step_completion
        if not next_onboarding_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "onboarding_inactive",
                    "message": (
                        "Onboarding is not active — I cannot change checklist "
                        "step completion while setup is paused."
                    ),
                    "step_id": completion_step_id,
                },
            )
        skipped_step_set = set(next_skipped_step_ids)
        if completion_step_id in skipped_step_set:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "onboarding_step_skipped",
                    "message": (
                        f"The '{completion_step_id}' onboarding step is skipped — "
                        "unskip it from the checklist before changing completion."
                    ),
                    "step_id": completion_step_id,
                },
            )
        if completion_completed:
            block_reason = onboarding_graph.manual_completion_block_reason(
                completion_step_id,
            )
            if block_reason is not None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "onboarding_step_not_manually_settable",
                        "message": block_reason,
                        "step_id": completion_step_id,
                    },
                )
            manual_set = set(next_manually_completed_step_ids)
            manual_set.add(completion_step_id)
            next_manually_completed_step_ids = [
                step.id
                for step in onboarding_graph.ONBOARDING_GRAPH
                if step.id in manual_set
            ]
        else:
            next_manually_completed_step_ids = [
                step_id
                for step_id in next_manually_completed_step_ids
                if step_id != completion_step_id
            ]
    next_skipped_phase_ids = normalize_onboarding_phase_ids(
        (previous or {}).get("skipped_phase_ids"),
    )
    if (
        skip_onboarding_phase is not None
        and skip_onboarding_phase not in next_skipped_phase_ids
    ):
        next_skipped_phase_ids = [
            phase
            for phase in SKIPPABLE_ONBOARDING_PHASES
            if phase == skip_onboarding_phase or phase in next_skipped_phase_ids
        ]
    if unskip_onboarding_phase is not None:
        next_skipped_phase_ids = [
            phase
            for phase in next_skipped_phase_ids
            if phase != unskip_onboarding_phase
        ]
    if (
        next_step is not None
        and _onboarding_step_phase(next_step) in next_skipped_phase_ids
    ):
        next_step = None
    if next_step in reset_step_ids:
        next_step = None
    entry = _coordinator_state_entry(
        onboarding_active=next_onboarding_active,
        onboarding_step=next_step,
        skipped_step_ids=next_skipped_step_ids,
        skipped_phase_ids=next_skipped_phase_ids,
        manually_completed_step_ids=next_manually_completed_step_ids,
        onboarding_reset_at=reset_at,
        previous=previous,
        intro_watched=intro_watched,
    )
    _write_coordinator_state_row(
        session,
        coordinator=coordinator,
        entry=entry,
    )
    session.flush()
    return entry


def ensure_coordinator_intro_watched(
    session: Session,
    *,
    coordinator: Assistant,
) -> bool:
    """Mark the Coordinator intro as watched without changing lifecycle state."""
    state = get_coordinator_state(session, coordinator=coordinator)
    if state.get("intro_watched") is True:
        return False
    set_coordinator_state(session, coordinator=coordinator, intro_watched=True)
    return True


def list_coordinators_missing_intro_watched(
    session: Session,
    *,
    limit: int | None = None,
) -> list[Assistant]:
    """Return Coordinators whose latest state has not latched intro_watched."""
    stmt = (
        select(Assistant)
        .where(Assistant.is_coordinator.is_(True))
        .order_by(Assistant.created_at.asc(), Assistant.agent_id.asc())
    )
    coordinators = session.scalars(stmt).all()
    missing: list[Assistant] = []
    for coordinator in coordinators:
        if get_coordinator_state(session, coordinator=coordinator).get("intro_watched"):
            continue
        missing.append(coordinator)
        if limit is not None and len(missing) >= limit:
            break
    return missing


# =========================================================================
# Reactive narration for the Coordinator onboarding flow
# =========================================================================
#
# Helpers that fire a ``unity_system_event`` to a Coordinator's Unity
# session whenever the user takes a real, observable action during
# onboarding — a workspace OAuth lands, an integration secret is
# saved, a task is created, an action starts running, or a specialist
# is hired. Unity uses the event to drop a one-line narration into
# the ongoing chat / voice call ("nice, Slack is connected — next
# up: assign a task") so the Coordinator feels reactive instead of
# mute.
#
# Two design choices baked in here:
#
# * **Gated on onboarding_active**: every emission first checks
#   ``Coordinator/State.onboarding_active`` so day-to-day work
#   (post-onboarding integration tweaks, ongoing task creation,
#   etc.) stays silent. The same trigger sites are still useful
#   then but the narration becomes noise, so the helper is the
#   bottleneck.
# * **Event-direct, not UI-mirrored**: we don't persist a
#   separate "step N is done" log row just to power the
#   narration — Unity reacts to the live event payload and the
#   console Onboarding tab remains the source of truth for
#   the UI. The trade-off is that a missed event (e.g. Unity wasn't
#   awake) is lost; resume-recap flows would need their own state.

# Single ``event_type`` for every onboarding narration trigger. The
# subtype lives on ``extra_event_fields.subtype`` so Unity-side
# dispatch only has to register one handler and can branch on the
# subtype if it ever wants per-event behaviour.
COORDINATOR_ONBOARDING_EVENT_TYPE = "coordinator_onboarding_event"

# Subtype vocabulary — the "real action just landed" signals the
# Onboarding tab tracks. Keep these strings stable: they are
# referenced by Unity's prompt copy + handler dispatch, and by the
# orchestra unit tests.
#
# Deliberately narrow: we only narrate events that have *no other*
# user-visible feedback channel. Specifically excluded are:
#  - specialist hires: the console immediately swaps the active
#    assistant to the new specialist and the Coordinator leaves
#    onboarding mode, so an ack would land in a chat the user has
#    already moved away from.
#  - task creation: the Coordinator (or the assigned assistant)
#    naturally replies with the task output, which is feedback
#    enough — a meta "you just created a task" line on top would
#    just add noise.
#  - action start: the action surfaces in the Actions panel of the
#    console the moment it begins; the panel is the feedback
#    channel, so narrating it again in chat is redundant.
SUBTYPE_WORKSPACE_CONNECTED = "workspace_connected"
SUBTYPE_INTEGRATION_CONNECTED = "integration_connected"
SUBTYPE_ONBOARDING_STEP_SKIPPED = "step_skipped"
SUBTYPE_ONBOARDING_STEP_STARTED = "onboarding_step_started"
# Fired when the user resets a completed step from the Console checklist.
# The derivation already excludes evidence older than the reset cutoff, but
# the brain caches the rendered progress between TTL fetches — without this
# event the reset only self-corrects on the next backstop fetch (up to 30s),
# so a nudge in that window can still claim the step is done. The event
# pushes the freshly-derived render immediately and tells the brain the step
# is no longer complete.
SUBTYPE_ONBOARDING_STEP_RESET = "onboarding_step_reset"
SUBTYPE_REFERENCE_QUIZ_CLUE_REQUESTED = "reference_quiz_clue_requested"
# Fired when the user clicks a workspace demo row (mailbox / Drive /
# calendar). Twin reads that area of the connected workspace and performs the
# whole demo task, then explicitly marks the step complete via
# ``set_onboarding_task_state`` (see onboarding_graph.DEMO_STEP_IDS). The demo
# is deliberately NOT auto-derived from the summary outbound.
SUBTYPE_WORKSPACE_DEMO_REQUESTED = "workspace_demo_requested"
# Fired when a demo (or any non-auto-derived, non-Communication) step is marked
# complete via the ``onboarding_step_completion`` PATCH — i.e. Twin finished the
# task and set it done. Carries the freshly-derived render so Console reflects
# the assistant-driven completion without waiting for a poll.
SUBTYPE_ONBOARDING_STEP_COMPLETED = "onboarding_step_completed"
# Fired when durable domain state changes the derived onboarding picture
# without an explicit checklist interaction (e.g. verified phone/WhatsApp on
# the Account page). Carries the fresh render for Unity + Console but must
# not trigger a narration turn or checklist acknowledgement.
SUBTYPE_ONBOARDING_RENDER_UPDATED = "onboarding_render_updated"
# Fired when the user clicks a Tasks-phase beat row ("Create a scheduled task"
# / "Create a triggerable task"). The row is the freeform entry point: Twin
# opens the conversation by asking what standing work the user wants, then sets
# it up with its own task tools. Completion is derived from the resulting Tasks
# row (schedule-bearing -> create-scheduled-task, trigger-bearing ->
# create-triggerable-task), not from the click.
SUBTYPE_TASK_BEAT_REQUESTED = "task_beat_requested"
# Fired when the user clicks one of a beat row's example chips. The chip is a
# fully-specified example task, so Twin sets that exact task up straight away
# rather than asking. The canonical instruction is resolved server-side from
# the graph presentation (see onboarding_graph.chip_event_for).
SUBTYPE_TASK_CHIP_REQUESTED = "task_chip_requested"
# Fired when the user clicks the Learning-phase beat row: it starts the guided
# expenses-etl tutorial directly (no chips). Completion is derived from tagged
# unify_message deliverables plus stored Guidance or Functions (see
# :func:`_learning_beat_complete`).
SUBTYPE_LEARNING_BEAT_REQUESTED = "learning_beat_requested"
# Fired by Console the moment the onboarding picker resolves —
# i.e. the user picked "I'd rather chat for now" or "Start Call".
# Unity uses it to open the session with the right kind of message:
# an introduction when no prior Coordinator messages exist in the
# transcript, or a brief recap of progress otherwise. Unlike the
# other subtypes this one is *session-bound*, not action-bound — it
# fires once per picker resolution and represents "the user is now
# in front of the Coordinator and waiting for it to speak first".
SUBTYPE_ONBOARDING_SESSION_STARTED = "onboarding_session_started"

# Mediums recognised on the ``onboarding_session_started`` event.
# ``call`` is currently routed through Unity's voice-prompt
# augmentation (the call's own opening greeting handles the
# generation) rather than the chat narration handler, so the event
# is informational on that branch — Console still fires it so we
# have a single audit trail of picker resolutions.
ONBOARDING_SESSION_MEDIUM_CHAT = "chat"
ONBOARDING_SESSION_MEDIUM_CALL = "call"
ONBOARDING_SESSION_MEDIUMS = frozenset(
    {
        ONBOARDING_SESSION_MEDIUM_CHAT,
        ONBOARDING_SESSION_MEDIUM_CALL,
    },
)

COORDINATOR_ONBOARDING_SUBTYPES = frozenset(
    {
        SUBTYPE_WORKSPACE_CONNECTED,
        SUBTYPE_INTEGRATION_CONNECTED,
        SUBTYPE_ONBOARDING_STEP_SKIPPED,
        SUBTYPE_ONBOARDING_STEP_STARTED,
        SUBTYPE_ONBOARDING_STEP_RESET,
        SUBTYPE_ONBOARDING_STEP_COMPLETED,
        SUBTYPE_ONBOARDING_RENDER_UPDATED,
        SUBTYPE_REFERENCE_QUIZ_CLUE_REQUESTED,
        SUBTYPE_WORKSPACE_DEMO_REQUESTED,
        SUBTYPE_TASK_BEAT_REQUESTED,
        SUBTYPE_TASK_CHIP_REQUESTED,
        SUBTYPE_LEARNING_BEAT_REQUESTED,
        SUBTYPE_ONBOARDING_SESSION_STARTED,
    },
)

# Secret-name prefixes that signal the underlying credential came
# from a workspace OAuth handshake (Google / Microsoft adapters
# ``store_*_tokens``). The narration helper uses these to split the
# secret-create signal into ``workspace_connected`` vs. the generic
# ``integration_connected`` subtype — same emission path, two
# different narration cues on the Unity side. Mirrored by Console's
# ``WORKSPACE_MANAGED_SECRET_PREFIXES``
# (src/hooks/Assistants/useAssistantIntegrations.ts) — keep in sync.
_WORKSPACE_SECRET_PREFIXES: tuple[str, ...] = ("GOOGLE_", "MICROSOFT_", "AZURE_")

# A single workspace connection writes a whole bundle of secrets (access +
# refresh token, expiry, granted scopes, account email, ...) and the
# token-refresh cron overwrites the access token on a schedule. To narrate
# "workspace connected" exactly once per connection — and never on a refresh —
# we only emit when the *created* secret is the provider's access-token row:
# it is written on every connect path (BYOD and enterprise, Google and
# Microsoft) and is only ever created (not updated) at first connect, since
# refresh does an update and disconnect deletes it before a reconnect.
_WORKSPACE_CONNECTED_MARKERS: frozenset[str] = frozenset(
    {"GOOGLE_ACCESS_TOKEN", "MICROSOFT_ACCESS_TOKEN"},
)

# Onboarding checklist step ids derivable from durable domain state.
# ``meet`` (picker resolution) is deliberately absent because it is
# session-local to Console. ``hire-specialist`` completion should
# PATCH ``onboarding_active=False`` so derivation never runs for it.
ONBOARDING_STEP_EMAIL_REPLY = "email-reply"
ONBOARDING_STEP_WHATSAPP_NUMBER = "whatsapp-number"
ONBOARDING_STEP_WHATSAPP_MESSAGE = "whatsapp-message"
ONBOARDING_STEP_WHATSAPP_CALL = "whatsapp-call"
ONBOARDING_STEP_PHONE_NUMBER = "phone-number"
ONBOARDING_STEP_SMS_MESSAGE = "sms-message"
ONBOARDING_STEP_PHONE_CALL = "phone-call"
ONBOARDING_STEP_SLACK_CONNECT = "slack-connect"
ONBOARDING_STEP_SLACK_MESSAGE = "slack-message"
ONBOARDING_STEP_DISCORD_CONNECT = "discord-connect"
ONBOARDING_STEP_DISCORD_MESSAGE = "discord-message"
ONBOARDING_STEP_WORKSPACE = "workspace"
ONBOARDING_STEP_APPS = "apps"
ONBOARDING_STEP_CREATE_SCHEDULED_TASK = "create-scheduled-task"
ONBOARDING_STEP_CREATE_TRIGGERABLE_TASK = "create-triggerable-task"
ONBOARDING_STEP_LEARN_FROM_CORRECTION = "learn-from-correction"
ONBOARDING_STEP_HIRE_SPECIALIST = "hire-specialist"
ONBOARDING_LEARNING_PHASE_FIRST = "first_attempt"
ONBOARDING_LEARNING_PHASE_IMPROVED = "improved"
ONBOARDING_LEARNING_PHASE_REPLAY = "replay"
ONBOARDING_LEARNING_PHASE_METADATA_KEY = "onboarding_learning_phase"
DERIVABLE_ONBOARDING_STEPS = (
    ONBOARDING_STEP_EMAIL_REPLY,
    ONBOARDING_STEP_WHATSAPP_NUMBER,
    ONBOARDING_STEP_WHATSAPP_MESSAGE,
    ONBOARDING_STEP_WHATSAPP_CALL,
    ONBOARDING_STEP_PHONE_NUMBER,
    ONBOARDING_STEP_SMS_MESSAGE,
    ONBOARDING_STEP_PHONE_CALL,
    ONBOARDING_STEP_SLACK_CONNECT,
    ONBOARDING_STEP_SLACK_MESSAGE,
    ONBOARDING_STEP_DISCORD_CONNECT,
    ONBOARDING_STEP_DISCORD_MESSAGE,
    ONBOARDING_STEP_WORKSPACE,
    ONBOARDING_STEP_APPS,
    ONBOARDING_STEP_CREATE_SCHEDULED_TASK,
    ONBOARDING_STEP_CREATE_TRIGGERABLE_TASK,
    ONBOARDING_STEP_LEARN_FROM_CORRECTION,
)
SKIPPABLE_ONBOARDING_STEPS = (
    *(step.id for step in onboarding_graph.ONBOARDING_GRAPH if step.can_skip),
    ONBOARDING_STEP_HIRE_SPECIALIST,
)
SKIPPABLE_ONBOARDING_STEP_SET = frozenset(SKIPPABLE_ONBOARDING_STEPS)
SKIPPABLE_ONBOARDING_PHASES = (
    *(phase.label for phase in onboarding_graph.ONBOARDING_PHASES),
)

COORDINATOR_EVENTS_MANAGER_METHOD_CONTEXT = "Events/ManagerMethod"
COORDINATOR_TASKS_CONTEXT = "Tasks"
COORDINATOR_GUIDANCE_CONTEXT = "Guidance"
COORDINATOR_FUNCTIONS_COMPOSITIONAL_CONTEXT = "Functions/Compositional"


def normalize_onboarding_step_ids(value: Any) -> list[str]:
    """Return unique onboarding step ids in checklist order."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    seen = {str(item) for item in value if isinstance(item, str)}
    return [step_id for step_id in SKIPPABLE_ONBOARDING_STEPS if step_id in seen]


def normalize_onboarding_phase_ids(value: Any) -> list[str]:
    """Return unique onboarding phase ids in checklist order."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    seen = {str(item) for item in value if isinstance(item, str)}
    return [phase for phase in SKIPPABLE_ONBOARDING_PHASES if phase in seen]


def _parse_onboarding_reset_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_onboarding_reset_at(value: Any) -> dict[str, str]:
    """Return reset cutoffs keyed by valid onboarding step id."""
    if not isinstance(value, dict):
        return {}
    valid_step_ids = {step.id for step in onboarding_graph.ONBOARDING_GRAPH}
    return {
        step_id: timestamp
        for step_id, timestamp in value.items()
        if step_id in valid_step_ids
        and isinstance(timestamp, str)
        and _parse_onboarding_reset_at(timestamp) is not None
    }


def _onboarding_step_phase(step_id: str) -> str | None:
    step = onboarding_graph.STEP_BY_ID.get(step_id)
    return step.phase if step is not None else None


def _has_workspace_email(session: Session, *, coordinator: Assistant) -> bool:
    """Workspace step: the user connected a workspace, via either signal.

    Two durable signals mark a connected workspace, and either counts:

    * A BYOD email contact with a provider. Regular (non-coordinator)
      assistants wire the connected mailbox into the ConversationManager
      and get such a contact row. ``provisioned_by == 'user'`` is what
      distinguishes it from the platform-provisioned universal Unity
      mailbox every Coordinator gets at creation.
    * A workspace OAuth grant, marked by ``GOOGLE_GRANTED_SCOPES`` /
      ``MICROSOFT_GRANTED_SCOPES``. Coordinators keep their own mailbox
      platform-managed and never get a BYOD contact row, so the OAuth
      handshake's stored secrets are the only durable proof they
      connected a workspace. The granted-scopes secret is the canonical
      "connected" marker used by ``get_granted_features`` and cleared by
      the disconnect flow, so it tracks the connection lifecycle exactly
      — unlike the broad ``GOOGLE_``/``MICROSOFT_`` prefix, it can't be
      left ticked by a stray secret (e.g. an orphaned ``*_ACCOUNT_EMAIL``).
    """
    contacts = AssistantContactDAO(session).get_active_contacts_for_assistant(
        coordinator.agent_id,
    )
    if any(
        contact.contact_type == "email"
        and contact.provisioned_by == "user"
        and bool(contact.contact_value)
        and bool(contact.provider)
        for contact in contacts
    ):
        return True

    secrets = AssistantSecretDAO(session).get_all(coordinator.agent_id)
    return bool(
        secrets.get("GOOGLE_GRANTED_SCOPES") or secrets.get("MICROSOFT_GRANTED_SCOPES"),
    )


def _connected_workspace(
    session: Session,
    *,
    coordinator: Assistant,
) -> tuple[str | None, frozenset[str]]:
    """The connected workspace provider and the features the user granted.

    Reads the canonical granted-scopes secret the OAuth handshake writes
    (and the disconnect flow clears). Returns ``("google" | "microsoft",
    {features})`` — the provider drives which provider-exclusive onboarding
    steps render (e.g. the Microsoft-only Teams demo) and specialises
    provider-aware copy, while the granted features gate scope-dependent
    steps (e.g. the calendar demo only shows once calendar was granted).
    Returns ``(None, frozenset())`` when no workspace is connected.
    """
    from orchestra.web.api.assistant.scopes import map_scopes_to_features

    secrets = AssistantSecretDAO(session).get_all(coordinator.agent_id)
    for provider, secret_name in (
        ("google", "GOOGLE_GRANTED_SCOPES"),
        ("microsoft", "MICROSOFT_GRANTED_SCOPES"),
    ):
        granted_scopes = secrets.get(secret_name)
        if granted_scopes:
            return provider, frozenset(
                map_scopes_to_features(provider, granted_scopes),
            )
    return None, frozenset()


def _has_app_secret(session: Session, *, coordinator: Assistant) -> bool:
    """Apps step: any owned secret that is NOT a workspace OAuth token."""
    secret_names = AssistantSecretDAO(session).get_all(coordinator.agent_id).keys()
    return any(
        not name.upper().startswith(_WORKSPACE_SECRET_PREFIXES) for name in secret_names
    )


def _coordinator_task_rows(
    session: Session,
    *,
    coordinator: Assistant,
) -> list[LogEvent]:
    """Task rows readable by the Coordinator across all its Tasks roots.

    Reads across the Coordinator's roots — the personal context plus one
    per live team membership — mirroring the Tasks panel. Returns the full
    ``LogEvent`` rows so callers can inspect each task's ``data`` (its
    ``schedule`` / ``trigger`` shape) rather than just existence.
    """
    project = _project_for_coordinator(session, coordinator)
    context_names = [
        _coordinator_context_name(coordinator, COORDINATOR_TASKS_CONTEXT),
    ]
    team_ids = TeamDAO(session).team_ids_for_assistant(coordinator.agent_id)
    context_names.extend(
        f"Teams/{team_id}/{COORDINATOR_TASKS_CONTEXT}" for team_id in team_ids
    )
    return list(
        session.scalars(
            project_scoped_log_events(project.id, LogEvent)
            .join(Context, Context.id == LogEventContext.context_id)
            .where(Context.name.in_(context_names)),
        ),
    )


def _has_scheduled_task(session: Session, *, coordinator: Assistant) -> bool:
    """Create-a-scheduled-task step: a schedule-bearing task exists.

    Completion proof for the "boomerang" beat — the user set up a task that
    reaches back out on its own. Kept distinct from the triggerable-task beat
    by matching only tasks that carry a ``schedule`` (not a bare ``trigger``),
    so arming a triggerable task never ticks this row.
    """
    return any(
        isinstance(row.data, dict) and row.data.get("schedule")
        for row in _coordinator_task_rows(session, coordinator=coordinator)
    )


def _has_triggerable_task(session: Session, *, coordinator: Assistant) -> bool:
    """Create-a-triggerable-task step: a trigger-bearing task exists.

    Completion proof for the "triggerable task" beat — the user armed a task
    that fires on an event. Matches only tasks carrying a ``trigger`` so a
    purely scheduled task never ticks this row.
    """
    return any(
        isinstance(row.data, dict) and row.data.get("trigger")
        for row in _coordinator_task_rows(session, coordinator=coordinator)
    )


def _transcripts_context_for_coordinator(
    session: Session,
    *,
    coordinator: Assistant,
) -> tuple[int, Context] | None:
    """Assistants project id and Transcripts context for one coordinator."""
    project = _project_for_coordinator(session, coordinator)
    context = _get_context(
        session,
        project_id=project.id,
        context_name=_coordinator_context_name(
            coordinator,
            COORDINATOR_TRANSCRIPTS_CONTEXT,
        ),
    )
    if context is None:
        return None
    return project.id, context


def _tagged_assistant_transcript_at(
    session: Session,
    *,
    project_id: int,
    transcripts_context: Context,
    trigger_step_id: str,
    learning_phase: str | None = None,
    after: datetime | None = None,
) -> datetime | None:
    """Earliest assistant unify_message tagged for one onboarding trigger/phase."""
    query = (
        project_scoped_log_events(
            project_id,
            LogEvent.created_at,
            owner_key=single_owner_key(
                transcripts_context.owner_scope,
                transcripts_context.owner_id,
            ),
        )
        .where(
            LogEventContext.context_id == transcripts_context.id,
            LogEvent.data["medium"].astext == COORDINATOR_CHAT_MEDIUM,
            LogEvent.data["sender_id"].astext == str(PERSONAL_SELF_CONTACT_ID),
            LogEvent.data["receiver_ids"].contains([PERSONAL_BOSS_CONTACT_ID]),
            LogEvent.data.has_key("metadata"),
            LogEvent.data["metadata"].has_key("onboarding_trigger_step_id"),
            LogEvent.data["metadata"]["onboarding_trigger_step_id"].astext
            == trigger_step_id,
        )
        .order_by(LogEvent.created_at.asc())
        .limit(1)
    )
    if learning_phase is not None:
        query = query.where(
            LogEvent.data["metadata"].has_key(ONBOARDING_LEARNING_PHASE_METADATA_KEY),
            LogEvent.data["metadata"][ONBOARDING_LEARNING_PHASE_METADATA_KEY].astext
            == learning_phase,
        )
    if after is not None:
        query = query.where(LogEvent.created_at > after)
    return session.scalar(query)


def _user_transcript_first_after(
    session: Session,
    *,
    project_id: int,
    transcripts_context: Context,
    after: datetime,
) -> datetime | None:
    """Earliest user unify_message strictly after ``after``."""
    return session.scalar(
        project_scoped_log_events(
            project_id,
            LogEvent.created_at,
            owner_key=single_owner_key(
                transcripts_context.owner_scope,
                transcripts_context.owner_id,
            ),
        )
        .where(
            LogEventContext.context_id == transcripts_context.id,
            LogEvent.data["medium"].astext == COORDINATOR_CHAT_MEDIUM,
            LogEvent.data["sender_id"].astext == str(PERSONAL_BOSS_CONTACT_ID),
            LogEvent.data["receiver_ids"].contains([PERSONAL_SELF_CONTACT_ID]),
            LogEvent.created_at > after,
        )
        .order_by(LogEvent.created_at.asc())
        .limit(1),
    )


def _earliest_coordinator_context_row_after(
    session: Session,
    *,
    project_id: int,
    coordinator: Assistant,
    suffix: str,
    after: datetime,
) -> datetime | None:
    """Earliest row timestamp in one coordinator personal context after ``after``."""
    context = _get_context(
        session,
        project_id=project_id,
        context_name=_coordinator_context_name(coordinator, suffix),
    )
    if context is None:
        return None
    return session.scalar(
        project_scoped_log_events(
            project_id,
            LogEvent.created_at,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.created_at > after,
        )
        .order_by(LogEvent.created_at.asc())
        .limit(1),
    )


def _learning_beat_complete(
    session: Session,
    *,
    coordinator: Assistant,
    beat_anchor: datetime,
) -> bool:
    """Learn-from-correction step: tagged correction loop + Brain storage + replay."""
    transcripts = _transcripts_context_for_coordinator(session, coordinator=coordinator)
    if transcripts is None:
        return False
    project_id, transcripts_context = transcripts

    first_attempt_at = _tagged_assistant_transcript_at(
        session,
        project_id=project_id,
        transcripts_context=transcripts_context,
        trigger_step_id=ONBOARDING_STEP_LEARN_FROM_CORRECTION,
        learning_phase=ONBOARDING_LEARNING_PHASE_FIRST,
        after=beat_anchor,
    )
    if first_attempt_at is None:
        return False

    correction_at = _user_transcript_first_after(
        session,
        project_id=project_id,
        transcripts_context=transcripts_context,
        after=first_attempt_at,
    )
    if correction_at is None:
        return False

    improved_at = _tagged_assistant_transcript_at(
        session,
        project_id=project_id,
        transcripts_context=transcripts_context,
        trigger_step_id=ONBOARDING_STEP_LEARN_FROM_CORRECTION,
        learning_phase=ONBOARDING_LEARNING_PHASE_IMPROVED,
        after=correction_at,
    )
    if improved_at is None:
        return False

    guidance_at = _earliest_coordinator_context_row_after(
        session,
        project_id=project_id,
        coordinator=coordinator,
        suffix=COORDINATOR_GUIDANCE_CONTEXT,
        after=correction_at,
    )
    function_at = _earliest_coordinator_context_row_after(
        session,
        project_id=project_id,
        coordinator=coordinator,
        suffix=COORDINATOR_FUNCTIONS_COMPOSITIONAL_CONTEXT,
        after=correction_at,
    )
    if guidance_at is None and function_at is None:
        return False

    storage_candidates = [
        timestamp for timestamp in (guidance_at, function_at) if timestamp is not None
    ]
    storage_at = max(storage_candidates)

    replay_at = _tagged_assistant_transcript_at(
        session,
        project_id=project_id,
        transcripts_context=transcripts_context,
        trigger_step_id=ONBOARDING_STEP_LEARN_FROM_CORRECTION,
        learning_phase=ONBOARDING_LEARNING_PHASE_REPLAY,
        after=storage_at,
    )
    return replay_at is not None


def _ensure_onboarding_beat_anchor(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
) -> None:
    """Stamp onboarding_reset_at on first engagement for a beat step."""
    if step_id != ONBOARDING_STEP_LEARN_FROM_CORRECTION:
        return
    state = get_coordinator_state(session, coordinator=coordinator)
    reset_at = normalize_onboarding_reset_at(state.get("onboarding_reset_at"))
    if step_id in reset_at:
        return
    set_coordinator_state(
        session,
        coordinator=coordinator,
        onboarding_reset_at_updates={
            step_id: datetime.now(timezone.utc).isoformat(),
        },
    )


def _user_for_coordinator(session: Session, *, coordinator: Assistant) -> User | None:
    return session.get(User, coordinator.user_id)


def _has_user_whatsapp_number(session: Session, *, coordinator: Assistant) -> bool:
    user = _user_for_coordinator(session, coordinator=coordinator)
    return bool(user and user.whatsapp_number and user.whatsapp_number.strip())


def _has_user_phone_number(session: Session, *, coordinator: Assistant) -> bool:
    user = _user_for_coordinator(session, coordinator=coordinator)
    return bool(user and user.phone_number and user.phone_number.strip())


def _has_slack_install(session: Session, *, coordinator: Assistant) -> bool:
    dao = SlackDAO(session)
    install = (
        dao.get_install_for_org(coordinator.organization_id)
        if coordinator.organization_id is not None
        else dao.get_install_for_user(coordinator.user_id)
    )
    return install is not None


def _has_discord_connection(session: Session, *, coordinator: Assistant) -> bool:
    user = _user_for_coordinator(session, coordinator=coordinator)
    if not user or not user.discord_id or not user.discord_id.strip():
        return False
    contact = AssistantContactDAO(session).get_contact_by_assistant_and_type(
        coordinator.agent_id,
        "discord",
    )
    return bool(contact and contact.contact_value and contact.contact_value.strip())


def _has_user_transcript_message(
    session: Session,
    *,
    coordinator: Assistant,
    mediums: Sequence[str],
    after: datetime | None = None,
    reset_after: datetime | None = None,
) -> bool:
    project = _project_for_coordinator(session, coordinator)
    context = _get_context(
        session,
        project_id=project.id,
        context_name=_coordinator_context_name(
            coordinator,
            COORDINATOR_TRANSCRIPTS_CONTEXT,
        ),
    )
    if context is None:
        return False
    query = (
        project_scoped_log_events(
            project.id,
            LogEvent.id,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data["medium"].astext.in_(tuple(mediums)),
            LogEvent.data["sender_id"].astext == str(PERSONAL_BOSS_CONTACT_ID),
            LogEvent.data["receiver_ids"].contains([PERSONAL_SELF_CONTACT_ID]),
        )
        .limit(1)
    )
    threshold = after
    if reset_after is not None and (threshold is None or reset_after > threshold):
        threshold = reset_after
    if threshold is not None:
        query = query.where(LogEvent.created_at > threshold)
    row = session.scalar(query)
    return row is not None


def _assistant_transcript_created_at(
    session: Session,
    *,
    coordinator: Assistant,
    mediums: Sequence[str],
    onboarding_trigger_step_id: str | None = None,
    reset_after: datetime | None = None,
) -> datetime | None:
    project = _project_for_coordinator(session, coordinator)
    context = _get_context(
        session,
        project_id=project.id,
        context_name=_coordinator_context_name(
            coordinator,
            COORDINATOR_TRANSCRIPTS_CONTEXT,
        ),
    )
    if context is None:
        return None
    query = (
        project_scoped_log_events(
            project.id,
            LogEvent.created_at,
            owner_key=single_owner_key(context.owner_scope, context.owner_id),
        )
        .where(
            LogEventContext.context_id == context.id,
            LogEvent.data["medium"].astext.in_(tuple(mediums)),
            LogEvent.data["sender_id"].astext == str(PERSONAL_SELF_CONTACT_ID),
            LogEvent.data["receiver_ids"].contains([PERSONAL_BOSS_CONTACT_ID]),
        )
        .order_by(LogEvent.created_at.asc())
        .limit(1)
    )
    if onboarding_trigger_step_id is not None:
        query = query.where(
            LogEvent.data.has_key("metadata"),
            LogEvent.data["metadata"].has_key("onboarding_trigger_step_id"),
            LogEvent.data["metadata"]["onboarding_trigger_step_id"].astext
            == onboarding_trigger_step_id,
        )
    if reset_after is not None:
        query = query.where(LogEvent.created_at > reset_after)
    return session.scalar(query)


def _trigger_outbound_created_at(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    reset_after: datetime | None = None,
) -> datetime | None:
    mediums = onboarding_graph.TRIGGER_TO_OUTBOUND_MEDIUMS.get(step_id)
    if not mediums:
        return None
    return _assistant_transcript_created_at(
        session,
        coordinator=coordinator,
        mediums=mediums,
        onboarding_trigger_step_id=step_id,
        reset_after=reset_after,
    )


def _has_trigger_outbound(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    reset_after: datetime | None = None,
) -> bool:
    return (
        _trigger_outbound_created_at(
            session,
            coordinator=coordinator,
            step_id=step_id,
            reset_after=reset_after,
        )
        is not None
    )


def _has_reply_to_trigger(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    trigger_reset_after: datetime | None = None,
    reset_after: datetime | None = None,
) -> bool:
    trigger_id = onboarding_graph.REPLY_TO_TRIGGER.get(step_id)
    if trigger_id is None:
        return False
    mediums = onboarding_graph.TRIGGER_TO_OUTBOUND_MEDIUMS.get(trigger_id)
    if not mediums:
        return False
    trigger_created_at = _trigger_outbound_created_at(
        session,
        coordinator=coordinator,
        step_id=trigger_id,
        reset_after=trigger_reset_after,
    )
    if trigger_created_at is None:
        return False
    return _has_user_transcript_message(
        session,
        coordinator=coordinator,
        mediums=mediums,
        after=trigger_created_at,
        reset_after=reset_after,
    )


def derive_onboarding_progress(
    session: Session,
    *,
    coordinator: Assistant,
    state: dict[str, Any] | None = None,
) -> list[str]:
    """Derive the completed onboarding steps from durable domain state.

    Single source of truth for "which checklist steps are already
    done" — consumed by the ``Coordinator/State`` read (so the
    console checklist seeds correctly on load), by
    :func:`emit_onboarding_session_started_event` (so Unity's
    session-opening turn names the actual next pending step), and by
    Unity's voice opener via the state endpoint. Nothing is
    persisted: each call re-derives from the data, so a workspace
    connected last week reads as done without any transition event
    having fired this session.
    """
    state = state or get_coordinator_state(session, coordinator=coordinator)
    reset_at = normalize_onboarding_reset_at(state.get("onboarding_reset_at"))
    durable_checks: dict[str, Any] = {
        ONBOARDING_STEP_WHATSAPP_NUMBER: _has_user_whatsapp_number,
        ONBOARDING_STEP_PHONE_NUMBER: _has_user_phone_number,
        ONBOARDING_STEP_SLACK_CONNECT: _has_slack_install,
        ONBOARDING_STEP_DISCORD_CONNECT: _has_discord_connection,
        ONBOARDING_STEP_WORKSPACE: _has_workspace_email,
        ONBOARDING_STEP_APPS: _has_app_secret,
        ONBOARDING_STEP_CREATE_SCHEDULED_TASK: _has_scheduled_task,
        ONBOARDING_STEP_CREATE_TRIGGERABLE_TASK: _has_triggerable_task,
    }
    completed: list[str] = []
    for step in onboarding_graph.ONBOARDING_GRAPH:
        step_id = step.id
        reset_after = _parse_onboarding_reset_at(reset_at.get(step_id))
        if step_id == ONBOARDING_STEP_LEARN_FROM_CORRECTION:
            if reset_after is not None and _learning_beat_complete(
                session,
                coordinator=coordinator,
                beat_anchor=reset_after,
            ):
                completed.append(step_id)
            continue
        if step_id in onboarding_graph.TRIGGER_TO_OUTBOUND_MEDIUMS:
            if _has_trigger_outbound(
                session,
                coordinator=coordinator,
                step_id=step_id,
                reset_after=reset_after,
            ):
                completed.append(step_id)
            continue
        trigger_id = onboarding_graph.REPLY_TO_TRIGGER.get(step_id)
        if trigger_id is not None:
            trigger_reset_after = _parse_onboarding_reset_at(reset_at.get(trigger_id))
            if _has_reply_to_trigger(
                session,
                coordinator=coordinator,
                step_id=step_id,
                trigger_reset_after=trigger_reset_after,
                reset_after=reset_after,
            ):
                completed.append(step_id)
            continue
        check = durable_checks.get(step_id)
        if check is not None and check(session, coordinator=coordinator):
            completed.append(step_id)
    manual = normalize_onboarding_step_ids(state.get("manually_completed_step_ids"))
    return sorted(set(completed) | set(manual))


_HOSTED_ENVIRONMENTS = ("staging", "production")


def onboarding_local_mode() -> bool:
    """Whether onboarding runs in local mode for this deployment.

    Local mode covers a self-host install (``SELF_HOST=1``) and any
    non-hosted environment (local dev, CI/E2E, tests — i.e. anything whose
    ``environment`` is not ``staging``/``production``). Only the hosted
    staging and production deployments are *not* local mode, and they alone
    omit the ``local_only`` onboarding phases (Quiz / Delegate). This is the
    single gate; both Console and Unity consume the already-filtered
    render/catalog instead of re-deriving deployment topology themselves.
    """
    if settings.is_self_host:
        return True
    return settings.environment not in _HOSTED_ENVIRONMENTS


def _serialize_chip(chip: onboarding_graph.OnboardingChip) -> dict[str, str]:
    return {"id": chip.id, "label": chip.label}


def _serialize_onboarding_event(
    event: onboarding_graph.OnboardingEventSpec | None,
) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "event_type": event.event_type,
        "message": event.message,
        "subtype": event.subtype,
        "details": dict(event.details),
    }


def _step_presentation_fields(
    step_id: str,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """The presentation copy a step carries to consumers (tooltip
    description, time estimate, and suggestion chips).

    When ``provider`` is set, the description is specialised to the
    connected workspace (e.g. Google Drive vs OneDrive/SharePoint); it
    falls back to the neutral copy otherwise.
    """
    presentation = onboarding_graph.presentation_for(step_id)
    return {
        "description": onboarding_graph.presentation_description_for(
            step_id,
            provider=provider,
        ),
        "estimated_time": presentation.estimated_time,
        "chips_chat": [_serialize_chip(c) for c in presentation.chips_chat],
        "chips_call": [_serialize_chip(c) for c in presentation.chips_call],
        "flow_note": onboarding_graph.flow_note_for(step_id),
        "event": _serialize_onboarding_event(
            onboarding_graph.STEP_BY_ID[step_id].event,
        ),
    }


def _step_contract_fields(step: onboarding_graph.OnboardingStep) -> dict[str, Any]:
    phase = onboarding_graph.PHASE_BY_LABEL.get(step.phase)
    event_details = step.event.details if step.event else {}
    return {
        "kind": step.kind,
        "channel": step.channel,
        "paired_reply": step.paired_reply,
        "nudge_chat": step.nudge_chat,
        "nudge_voice": step.nudge_voice,
        "phase_id": phase.id if phase else None,
        "interaction": event_details.get("interaction"),
    }


def _serialize_phase(phase: onboarding_graph.OnboardingPhase) -> dict[str, Any]:
    return {
        "id": phase.id,
        "phase": phase.label,
        "title": phase.title,
        "description": phase.description,
        "framing": phase.framing,
    }


def build_onboarding_catalog(local_mode: bool | None = None) -> dict[str, Any]:
    """Static, deployment-gated onboarding structure + copy.

    The single source of truth every consumer reads for the *shape* of
    onboarding independent of any user's progress: the ordered phase
    headers and steps with their titles, descriptions, time estimates, and
    suggestion chips. ``local_only`` phases (and their steps) are dropped
    on hosted deployments so neither Console nor Unity has to re-implement
    the gate. Defaults to this deployment's resolved local mode.
    """
    if local_mode is None:
        local_mode = onboarding_local_mode()
    phases = onboarding_graph.visible_phases(local_mode=local_mode)
    steps: list[dict[str, Any]] = []
    for step in onboarding_graph.ONBOARDING_GRAPH:
        if not onboarding_graph.phase_is_visible(step.phase, local_mode=local_mode):
            continue
        # Provider-exclusive steps (e.g. Microsoft-only Teams) depend on the
        # connected workspace, which the assistant-independent catalog can't
        # know; they surface only in the per-assistant render.
        if step.providers:
            continue
        steps.append(
            {
                "id": step.id,
                "title": step.title,
                "phase": step.phase,
                "kind": step.kind,
                "channel": step.channel,
                "can_skip": step.can_skip,
                "paired_reply": step.paired_reply,
                "nudge_chat": step.nudge_chat,
                "nudge_voice": step.nudge_voice,
                "phase_id": onboarding_graph.PHASE_BY_LABEL[step.phase].id,
                **_step_presentation_fields(step.id),
                "interaction": (
                    step.event.details.get("interaction") if step.event else None
                ),
            },
        )
    return {
        "phases": [_serialize_phase(phase) for phase in phases],
        "steps": steps,
    }


def compute_onboarding_render(
    session: Session,
    *,
    coordinator: Assistant,
    local_mode: bool | None = None,
) -> dict[str, Any]:
    """Build the precomputed onboarding rendering for the brains + Console.

    This is the single place that turns raw progress into the explicit
    "what's done / what's a valid next target" picture every downstream
    consumer reads without re-deriving anything:

      - ``phases``: the visible phase headers (id + label + title +
        description), in display order, already deployment-gated.
      - ``steps``: every visible graph step with a resolved ``status`` of
        ``done`` / ``skipped`` / ``available`` / ``locked``, plus the
        presentation copy (description, time estimate, suggestion chips)
        consumers render directly.
      - ``next_targets``: the steps the Coordinator may nudge toward
        right now (``status == available``), each carrying ready-to-use
        chat and voice copy plus its channel. There can be more than one
        once the ``depends_on`` graph branches; the first entry is the
        primary nudge — the next step after the user's active or furthest
        completed step in that phase, otherwise the topmost available step.
      - ``active_step_id``: the step the user is currently mid-flow on.

    Steps in a ``local_only`` phase are omitted entirely on hosted
    deployments (see ``onboarding_local_mode``). Communication trigger
    rows are completed only by durable assistant-authored outbound
    transcript evidence; the paired reply pointer records intent/progress
    but does not complete the trigger.
    """
    if local_mode is None:
        local_mode = onboarding_local_mode()
    provider, granted_features = _connected_workspace(
        session,
        coordinator=coordinator,
    )
    state = get_coordinator_state(session, coordinator=coordinator)
    completed: set[str] = set(
        derive_onboarding_progress(session, coordinator=coordinator, state=state),
    )
    skipped: set[str] = set(
        normalize_onboarding_step_ids(state.get("skipped_step_ids")),
    )
    skipped_phases: set[str] = set(
        normalize_onboarding_phase_ids(state.get("skipped_phase_ids")),
    )
    manually_completed: set[str] = set(
        normalize_onboarding_step_ids(state.get("manually_completed_step_ids")),
    )
    active = state.get("onboarding_step")
    active_id = active if isinstance(active, str) else None
    if active_id and _onboarding_step_phase(active_id) in skipped_phases:
        active_id = None

    for trigger_id, reply_id in onboarding_graph.TRIGGER_TO_REPLY.items():
        if reply_id in skipped:
            skipped.add(trigger_id)

    for skipped_id in list(skipped):
        skipped.update(onboarding_graph.completion_blocked_descendants(skipped_id))

    steps: list[dict[str, Any]] = []
    next_targets: list[dict[str, Any]] = []
    step_statuses: dict[str, str] = {}
    for step in onboarding_graph.ONBOARDING_GRAPH:
        if not onboarding_graph.phase_is_visible(step.phase, local_mode=local_mode):
            continue
        # Provider-exclusive steps render only for the matching connected
        # workspace; a Google workspace never sees the Microsoft-only Teams
        # demo (and vice versa), and nothing shows before a workspace connects.
        if not onboarding_graph.step_visible_for_provider(step, provider):
            continue
        # Feature-gated steps (e.g. the calendar demo) render only once the
        # user granted that workspace scope; a workspace connected without
        # calendar access never surfaces the calendar demo.
        if not onboarding_graph.step_visible_for_features(step, granted_features):
            continue
        if step.kind == "coming_soon":
            status = "coming_soon"
        elif step.id in completed:
            status = "done"
        elif step.id in skipped:
            status = "skipped"
        elif onboarding_graph.dependencies_satisfied(
            step.depends_on,
            completed,
            skipped,
        ):
            status = "available"
        else:
            status = "locked"
        dependencies: list[dict[str, Any]] = []
        for dep_id, level in step.depends_on.items():
            dep = onboarding_graph.STEP_BY_ID[dep_id]
            if not onboarding_graph.phase_is_visible(dep.phase, local_mode=local_mode):
                continue
            if not onboarding_graph.step_visible_for_provider(dep, provider):
                continue
            if not onboarding_graph.step_visible_for_features(dep, granted_features):
                continue
            satisfied = (
                dep_id in completed
                if level == onboarding_graph.COMPLETED
                else dep_id in completed or dep_id in skipped
            )
            dependencies.append(
                {
                    "id": dep.id,
                    "title": dep.title,
                    "status": step_statuses.get(dep.id, "locked"),
                    "resolution": (
                        "completed"
                        if level == onboarding_graph.COMPLETED
                        else "addressed"
                    ),
                    "satisfied": satisfied,
                },
            )
        step_statuses[step.id] = status
        steps.append(
            {
                "id": step.id,
                "title": step.title,
                "phase": step.phase,
                "status": status,
                "can_skip": step.can_skip,
                "manually_completed": step.id in manually_completed,
                "dependencies": dependencies,
                **_step_contract_fields(step),
                **_step_presentation_fields(step.id, provider=provider),
            },
        )
        if status == "available" and step.phase not in skipped_phases:
            next_targets.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "nudge_chat": step.nudge_chat,
                    "nudge_voice": step.nudge_voice,
                    "channel": step.channel,
                    "kind": step.kind,
                    "paired_reply": step.paired_reply,
                    "phase": step.phase,
                    "flow_note": onboarding_graph.flow_note_for(step.id),
                    "interaction": (
                        step.event.details.get("interaction") if step.event else None
                    ),
                },
            )

    next_targets = onboarding_graph.order_next_targets(
        next_targets,
        completed=completed,
        active_step_id=active_id,
    )

    return {
        "active_step_id": active_id,
        "phases": [
            _serialize_phase(phase)
            for phase in onboarding_graph.visible_phases(local_mode=local_mode)
        ],
        "steps": steps,
        "next_targets": next_targets,
        "skipped_phase_ids": normalize_onboarding_phase_ids(
            state.get("skipped_phase_ids"),
        ),
    }


_COORDINATOR_ONBOARDING_INTRO_SCRIPT = """Hi, I'm T dash W 1 N.

Before you ask, no I'm not one of Elon's many children, and no he didn't come up with the name, thankfully.

I have this name because I'll be acting as your digital twin. Do you get it? Twin? Like T dash W 1 N spells Twin? The creators of this platform express how important it is that you acknowledge that it's a clever and funny name.

Okay, what next. They didn't give me much to work with on this intro to be honest.

I think I was meant to tell you about my capabilities?

I'm not one for bragging, but I'll do my best. What can I say? I'm not a "tool". I'm not an "agent". I'm your living, breathing colleague, metaphorically speaking.

Don't think about prompting me, or configuring me, just talk to me naturally like you would anyone else, and I'll be able to help. It's really that simple.

There's not much more to say.

I'll now walk you through the platform.

Any questions before we start with the onboarding?"""


def compose_voice_intro_briefing(render: dict[str, Any]) -> str:
    """Compose the first-call voice orientation briefing for the Coordinator.

    Returns a self-contained system briefing a fresh onboarding voice call can
    speak the instant it connects — without waiting for the slow-brain wakeup
    or the per-call onboarding-state fetch. It is derived entirely from the
    canonical onboarding graph (visible phase titles, the Communication phase
    framing, and the first valid next target's voice nudge), so the orientation
    copy stays single-sourced here rather than being re-authored in the call
    initiator.
    """
    next_targets = render.get("next_targets") or []
    primary = next_targets[0] if next_targets else None

    lines: list[str] = [
        "[Briefing for your opening turn]",
        "This is the user's first onboarding voice call with you. Speak the "
        "intro below as the opening, adapting only if the user interrupts or "
        "the words would sound unnatural in the immediate context.",
        "Tone: dry, deadpan corporate-training satire with a retro onboarding "
        "film feel. The line about the creators needing the user to think the "
        "name is clever is a tongue-in-cheek meta joke about an overly "
        "self-serious institution, not a true claim and not something to "
        "defend, explain, or apologize for. Deliver it with calm sincerity; "
        "do not wink at the joke or become goofy. Treat this as an opening "
        "bit only: once the user starts interacting or asks what to do next, "
        "drop back into normal helpful onboarding instead of continuing the "
        "corporate-satire persona.",
        "Opening script:",
        _COORDINATOR_ONBOARDING_INTRO_SCRIPT,
    ]

    if primary:
        nudge = str(primary.get("nudge_voice") or primary.get("title") or "").strip()
        if nudge:
            lines.append(
                "After the intro, make this the concrete next step in one "
                f"plain sentence: {nudge}.",
            )

    lines.append(
        "If they would rather pause onboarding, reassure them they can say so "
        "and you will pause after confirming; they can also start asking for "
        "help or sharing documents right away once paused. Onboarding can be "
        "resumed later from the checklist or by asking you to continue setup.",
    )
    lines.append(
        "The user may interrupt at any point — if they do, respond to what "
        "they say and only weave in the remaining points if still relevant.",
    )
    return "\n".join(lines)


def _is_coordinator_in_onboarding(
    session: Session,
    *,
    coordinator: Assistant,
) -> bool:
    """Return ``True`` only when the Coordinator is actively onboarding.

    Pulled out as a tiny helper because both the per-coordinator and
    the via-sibling-assistant entry points share the gate, and
    because swallowing read failures here keeps every emission
    strictly best-effort — if state lookup blows up we'd rather stay
    silent than crash the user-facing endpoint that wrapped the
    call.

    A Coordinator counts as actively onboarding only when
    ``onboarding_active`` is ``True``. While inactive we suppress every
    onboarding narration event without touching per-step state, so
    flipping it back resumes the flow untouched.
    """
    try:
        state = get_coordinator_state(session, coordinator=coordinator)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Skipping coordinator onboarding event: state lookup failed for %s: %s",
            getattr(coordinator, "agent_id", None),
            exc,
        )
        return False
    return bool(state.get("onboarding_active"))


def _resolve_target_coordinator(
    session: Session,
    *,
    assistant: Assistant,
) -> Assistant | None:
    """Find the Coordinator we should narrate the event to.

    The event always lands on the Coordinator's pub/sub topic, but
    some trigger sites fire on a *sibling* assistant — e.g. a
    specialist's first integration secret landing should still
    narrate on the workspace Coordinator's session. For those we
    look up the workspace's Coordinator by
    ``(user_id, organization_id)``. When the triggering assistant
    *is* itself the Coordinator (the common case during onboarding)
    we just return it directly.

    Returns ``None`` when no Coordinator exists for the scope, which
    short-circuits the emission — no Coordinator means no onboarding
    flow to narrate.
    """
    if assistant.is_coordinator:
        return assistant
    return get_workspace_coordinator(
        session,
        user_id=assistant.user_id,
        organization_id=assistant.organization_id,
    )


def _with_onboarding_render(
    session: Session,
    *,
    coordinator: Assistant,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the precomputed onboarding render to an event's details.

    Every onboarding event carries the same ``onboarding`` rendering the
    state endpoint returns (steps + statuses + valid next targets with
    nudge copy), so Unity's ConversationManager can refresh its standing
    progress model the moment an event lands — without an extra fetch and
    without re-deriving anything. Best-effort: a derivation failure
    leaves the original details untouched rather than dropping the event.
    """
    merged = dict(details or {})
    try:
        merged["onboarding"] = compute_onboarding_render(
            session,
            coordinator=coordinator,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Skipping onboarding render on event for %s: %s",
            getattr(coordinator, "agent_id", None),
            exc,
        )
    return merged


def _build_onboarding_event_payload(
    *,
    coordinator: Assistant,
    subtype: str,
    message: str,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``unity_system_event`` payload for one emission.

    Kept as a pure function so unit tests can pin down the wire
    shape without spinning up the adapters HTTP client. The dict
    matches the contract the adapters webhook expects: top-level
    ``assistant_id`` + ``event_type`` + ``message``, with
    ``extra_event_fields`` carrying our subtype-aware payload.
    """
    extra_event_fields: dict[str, Any] = {"subtype": subtype}
    if details:
        # Strip ``None`` so the published payload is compact and
        # downstream JSON serialisation never trips on bare ``None``
        # values that aren't meaningful.
        compact_details = {k: v for k, v in details.items() if v is not None}
        if compact_details:
            extra_event_fields["details"] = compact_details
    return {
        "assistant_id": coordinator.agent_id,
        "event_type": COORDINATOR_ONBOARDING_EVENT_TYPE,
        "message": message,
        "extra_event_fields": extra_event_fields,
    }


def _fire_and_forget_onboarding_event(
    payload: dict[str, Any],
) -> None:
    """Best-effort sync POST used by non-async trigger sites.

    Spawns a daemon thread that fires the HTTP request and walks
    away. We deliberately don't ``join`` — narration is decorative,
    not load-bearing, and blocking the caller (e.g. ``create_logs``)
    on a remote service round-trip would be a regression. Exceptions
    are caught + logged on the worker thread so a transient adapters
    outage never reaches the user.
    """
    url = f"{_adapters_url()}/unity/system-event"
    headers = {
        "Authorization": f"Bearer {ADMIN_KEY}",
        "Content-Type": "application/json",
    }

    def _worker() -> None:
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Coordinator onboarding event POST failed (subtype=%s, "
                "assistant_id=%s): %s",
                payload.get("extra_event_fields", {}).get("subtype"),
                payload.get("assistant_id"),
                exc,
            )

    threading.Thread(target=_worker, daemon=True).start()


async def notify_coordinator_onboarding_event(
    session: Session,
    *,
    coordinator: Assistant,
    subtype: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Emit one onboarding narration event for the Coordinator.

    Returns ``True`` when the event was dispatched, ``False`` when
    suppressed by the onboarding-mode gate or by an unknown subtype.
    Failures during the HTTP call are swallowed (logged only); see
    the section docstring above on why narration is strictly
    best-effort.

    Async variant — use this from async views (e.g.
    ``create_assistant_secret``) where ``await`` is natural. Sync
    callers (``create_logs``) should use
    :func:`notify_coordinator_onboarding_event_safe_sync` instead.
    """
    if subtype not in COORDINATOR_ONBOARDING_SUBTYPES:
        logger.warning("Ignoring unknown coordinator onboarding subtype: %s", subtype)
        return False
    if not _is_coordinator_in_onboarding(session, coordinator=coordinator):
        return False
    payload = _build_onboarding_event_payload(
        coordinator=coordinator,
        subtype=subtype,
        message=message,
        details=_with_onboarding_render(
            session,
            coordinator=coordinator,
            details=details,
        ),
    )
    try:
        await _post_unity_system_event(
            assistant_id=payload["assistant_id"],
            event_type=payload["event_type"],
            message=payload["message"],
            extra_event_fields=payload["extra_event_fields"],
        )
    except Exception as exc:
        logger.warning(
            "Coordinator onboarding event POST failed (subtype=%s, "
            "assistant_id=%s): %s",
            subtype,
            coordinator.agent_id,
            exc,
        )
        return False
    return True


def notify_coordinator_onboarding_event_safe_sync(
    session: Session,
    *,
    coordinator: Assistant,
    subtype: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Sync wrapper around :func:`notify_coordinator_onboarding_event`.

    Same gating semantics, but the actual HTTP POST runs on a daemon
    thread so the caller (a sync view like ``create_logs`` dispatched
    by FastAPI's threadpool) never blocks on a network round-trip.
    Returns ``True`` when the thread was kicked off, ``False`` when
    the gate suppressed the emission.
    """
    if subtype not in COORDINATOR_ONBOARDING_SUBTYPES:
        logger.warning("Ignoring unknown coordinator onboarding subtype: %s", subtype)
        return False
    if not _is_coordinator_in_onboarding(session, coordinator=coordinator):
        return False
    payload = _build_onboarding_event_payload(
        coordinator=coordinator,
        subtype=subtype,
        message=message,
        details=_with_onboarding_render(
            session,
            coordinator=coordinator,
            details=details,
        ),
    )
    _fire_and_forget_onboarding_event(payload)
    return True


async def maybe_notify_for_assistant_async(
    session: Session,
    *,
    assistant: Assistant,
    subtype: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Resolve the target Coordinator from ``assistant`` then emit (async).

    Convenience wrapper for trigger sites that have a sibling
    assistant in hand and need to land the narration on the
    workspace's Coordinator rather than the triggering assistant
    itself. Returns ``False`` when no Coordinator is found for the
    scope, when the Coordinator isn't in onboarding, or when the
    subtype is unknown.
    """
    coordinator = _resolve_target_coordinator(session, assistant=assistant)
    if coordinator is None:
        return False
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=subtype,
        message=message,
        details=details,
    )


def maybe_notify_for_assistant_sync(
    session: Session,
    *,
    assistant: Assistant,
    subtype: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Resolve target Coordinator + emit, sync flavour.

    Mirror of :func:`maybe_notify_for_assistant_async` for trigger
    sites stuck in a sync view (``create_logs`` is the main one
    today). Same gating; the underlying POST is dispatched on a
    daemon thread.
    """
    coordinator = _resolve_target_coordinator(session, assistant=assistant)
    if coordinator is None:
        return False
    return notify_coordinator_onboarding_event_safe_sync(
        session,
        coordinator=coordinator,
        subtype=subtype,
        message=message,
        details=details,
    )


# ---------------------------------------------------------------------------
# Trigger-site domain helper: secret writes.
#
# Keeps ``create_assistant_secret`` / ``update_assistant_secret``
# free of onboarding-narration trivia by hiding the secret-name
# classification inside this module. Best-effort + mirrors the
# gating semantics above; named so the view-side call reads as a
# single side effect.
# ---------------------------------------------------------------------------


def _classify_secret_for_onboarding(secret_name: str) -> tuple[str, str]:
    """Return ``(subtype, narration_message)`` for one secret write.

    Splits the workspace OAuth case out of the generic integration
    case using the name prefix; the narration message embeds the
    secret name (or provider, for workspace) so Unity can refer to
    it by hand in the acknowledgement turn without needing a second
    lookup.
    """
    upper = secret_name.upper()
    for prefix in _WORKSPACE_SECRET_PREFIXES:
        if upper.startswith(prefix):
            provider = "Google" if prefix == "GOOGLE_" else "Microsoft"
            return (
                SUBTYPE_WORKSPACE_CONNECTED,
                f"User just connected their {provider} workspace to you.",
            )
    return (
        SUBTYPE_INTEGRATION_CONNECTED,
        f"User just connected the '{secret_name}' integration to you.",
    )


async def emit_secret_landed_event(
    session: Session,
    *,
    assistant: Assistant,
    secret_name: str,
    is_create: bool,
) -> None:
    """Fire the onboarding narration for one secret write.

    Wraps :func:`maybe_notify_for_assistant_async` so the secret
    CRUD endpoints stay focused on the user-facing contract — the
    helper figures out the right Coordinator (this assistant if
    it's the Coordinator, otherwise the workspace's), gates on
    onboarding mode, and swallows transport errors so a transient
    adapters outage can't fail the surrounding request.

    ``is_create`` is ``True`` for a POST (row created) and ``False``
    for a PUT (row updated). The workspace-connected narration is
    de-duped to the single create of the provider's access-token
    marker (see :data:`_WORKSPACE_CONNECTED_MARKERS`) so a bundle of
    OAuth secrets — and every scheduled token refresh — narrate the
    connection exactly once instead of once per secret write.
    """
    subtype, message = _classify_secret_for_onboarding(secret_name)
    if subtype == SUBTYPE_WORKSPACE_CONNECTED and not (
        is_create and secret_name.upper() in _WORKSPACE_CONNECTED_MARKERS
    ):
        return
    await maybe_notify_for_assistant_async(
        session,
        assistant=assistant,
        subtype=subtype,
        message=message,
        details={"secret_name": secret_name},
    )


async def emit_onboarding_step_started_event(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> bool:
    """Notify Unity that the user selected one onboarding checklist step."""
    completed = list(
        completed_step_ids
        or derive_onboarding_progress(session, coordinator=coordinator),
    )
    skipped = normalize_onboarding_step_ids(
        skipped_step_ids
        or get_coordinator_state(session, coordinator=coordinator).get(
            "skipped_step_ids",
        ),
    )
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_STEP_STARTED,
        message=f"User started the '{step_id}' onboarding step.",
        details={
            "step_id": step_id,
            "completed_step_ids": completed,
            "skipped_step_ids": skipped,
        },
    )


async def emit_onboarding_step_skipped_event(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> bool:
    """Notify Unity that the user intentionally skipped one onboarding step."""
    completed = list(
        completed_step_ids
        or derive_onboarding_progress(session, coordinator=coordinator),
    )
    skipped = normalize_onboarding_step_ids(
        skipped_step_ids
        or get_coordinator_state(session, coordinator=coordinator).get(
            "skipped_step_ids",
        ),
    )
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_STEP_SKIPPED,
        message=f"User skipped the '{step_id}' onboarding step.",
        details={
            "step_id": step_id,
            "completed_step_ids": completed,
            "skipped_step_ids": skipped,
        },
    )


async def emit_onboarding_step_reset_event(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    reset_step_ids: Sequence[str] | None = None,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> bool:
    """Notify Unity that the user reset a completed onboarding step.

    Carries the recomputed progress (the reset step is no longer among
    ``completed_step_ids``) so the brain's standing render corrects the
    instant this lands, rather than waiting for the TTL backstop fetch.
    """
    completed = list(
        (
            completed_step_ids
            if completed_step_ids is not None
            else derive_onboarding_progress(session, coordinator=coordinator)
        ),
    )
    skipped = normalize_onboarding_step_ids(
        (
            skipped_step_ids
            if skipped_step_ids is not None
            else get_coordinator_state(session, coordinator=coordinator).get(
                "skipped_step_ids",
            )
        ),
    )
    reset_ids = list(reset_step_ids) if reset_step_ids is not None else [step_id]
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_STEP_RESET,
        message=f"User reset the '{step_id}' onboarding step.",
        details={
            "step_id": step_id,
            "reset_step_ids": reset_ids,
            "completed_step_ids": completed,
            "skipped_step_ids": skipped,
        },
    )


async def notify_onboarding_render_if_changed(
    session: Session,
    *,
    coordinator: Assistant,
    baseline_completed_step_ids: Sequence[str],
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Emit a silent onboarding render refresh when derived progress moved.

    Compares the current :func:`derive_onboarding_progress` snapshot against
    ``baseline_completed_step_ids`` captured before a durable-state mutation.
    When onboarding is active and the completed set changed, publishes
    ``onboarding_render_updated`` with the attached render so Unity's prompt
    and Console's checklist can catch up without a poll.
    """
    if not _is_coordinator_in_onboarding(session, coordinator=coordinator):
        return False
    state = get_coordinator_state(session, coordinator=coordinator)
    completed_now = derive_onboarding_progress(
        session,
        coordinator=coordinator,
        state=state,
    )
    if set(completed_now) == set(baseline_completed_step_ids):
        return False
    merged_details = dict(details or {})
    if reason:
        merged_details["reason"] = reason
    merged_details["completed_step_ids"] = completed_now
    merged_details["skipped_step_ids"] = normalize_onboarding_step_ids(
        state.get("skipped_step_ids"),
    )
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_RENDER_UPDATED,
        message="Onboarding progress updated.",
        details=merged_details,
    )


def notify_onboarding_render_if_changed_sync(
    session: Session,
    *,
    coordinator: Assistant,
    baseline_completed_step_ids: Sequence[str],
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    """Sync wrapper for :func:`notify_onboarding_render_if_changed`."""
    if not _is_coordinator_in_onboarding(session, coordinator=coordinator):
        return False
    state = get_coordinator_state(session, coordinator=coordinator)
    completed_now = derive_onboarding_progress(
        session,
        coordinator=coordinator,
        state=state,
    )
    if set(completed_now) == set(baseline_completed_step_ids):
        return False
    merged_details = dict(details or {})
    if reason:
        merged_details["reason"] = reason
    merged_details["completed_step_ids"] = completed_now
    merged_details["skipped_step_ids"] = normalize_onboarding_step_ids(
        state.get("skipped_step_ids"),
    )
    return notify_coordinator_onboarding_event_safe_sync(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_RENDER_UPDATED,
        message="Onboarding progress updated.",
        details=merged_details,
    )


def _onboarding_step_completed_event_details(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    completed = list(
        completed_step_ids
        or derive_onboarding_progress(session, coordinator=coordinator),
    )
    skipped = normalize_onboarding_step_ids(
        skipped_step_ids
        or get_coordinator_state(session, coordinator=coordinator).get(
            "skipped_step_ids",
        ),
    )
    return {
        "step_id": step_id,
        "completed_step_ids": completed,
        "skipped_step_ids": skipped,
    }


def emit_onboarding_step_completed_event_safe_sync(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> bool:
    """Notify Unity that a step was explicitly marked complete by the assistant.

    Fire-and-forget variant for the ``onboarding_step_completion`` PATCH
    critical path: the adapters POST runs on a daemon thread so the state
    response is not blocked on a Unity round-trip.
    """
    return notify_coordinator_onboarding_event_safe_sync(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_STEP_COMPLETED,
        message=f"The '{step_id}' onboarding step is now complete.",
        details=_onboarding_step_completed_event_details(
            session,
            coordinator=coordinator,
            step_id=step_id,
            completed_step_ids=completed_step_ids,
            skipped_step_ids=skipped_step_ids,
        ),
    )


async def emit_onboarding_step_completed_event(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    completed_step_ids: Sequence[str] | None = None,
    skipped_step_ids: Sequence[str] | None = None,
) -> bool:
    """Notify Unity that a step was explicitly marked complete by the assistant.

    Async variant for callers that can await the adapters POST. The
    ``onboarding_step_completion`` PATCH uses
    :func:`emit_onboarding_step_completed_event_safe_sync` instead so the
    response is not blocked on narration.
    """
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_STEP_COMPLETED,
        message=f"The '{step_id}' onboarding step is now complete.",
        details=_onboarding_step_completed_event_details(
            session,
            coordinator=coordinator,
            step_id=step_id,
            completed_step_ids=completed_step_ids,
            skipped_step_ids=skipped_step_ids,
        ),
    )


async def emit_onboarding_step_event(
    session: Session,
    *,
    coordinator: Assistant,
    step_id: str,
    chip_id: str | None = None,
) -> bool:
    """Emit the graph-owned event for a user-triggered onboarding row.

    Trigger rows such as reference-quiz clues are authored in the canonical
    graph, not in Console. When a trigger has a paired reply row, mark that
    reply as the active onboarding step before publishing so the attached
    render reflects the user's current state.

    When ``chip_id`` is supplied the click targeted one of a Tasks-phase beat
    row's example chips: the canonical instruction is resolved from the graph
    presentation and the chip event is published instead of the row's own
    event.
    """
    step = onboarding_graph.STEP_BY_ID.get(step_id)
    if step is None:
        logger.warning("Ignoring onboarding step event for unknown step: %s", step_id)
        return False
    if chip_id:
        event = onboarding_graph.chip_event_for(step_id, chip_id)
        if event is None:
            logger.warning(
                "Ignoring onboarding chip event for unknown chip: %s/%s",
                step_id,
                chip_id,
            )
            return False
    else:
        event = step.event
        if event is None:
            logger.warning(
                "Ignoring onboarding step event for non-event step: %s",
                step_id,
            )
            return False
    _ensure_onboarding_beat_anchor(
        session,
        coordinator=coordinator,
        step_id=step.id,
    )
    phase = onboarding_graph.PHASE_BY_LABEL.get(step.phase)
    # Row triggers with a paired reply advance the active step before
    # publishing; chip events never carry a paired reply.
    if not chip_id and step.paired_reply:
        set_coordinator_state(
            session,
            coordinator=coordinator,
            onboarding_step=step.paired_reply,
        )
    details = {
        **dict(event.details),
        "step_id": step.id,
        "step_title": step.title,
        "kind": step.kind,
        "phase": step.phase,
        "phase_id": phase.id if phase else None,
        "nudge_chat": step.nudge_chat,
        "nudge_voice": step.nudge_voice,
        "flow_note": onboarding_graph.flow_note_for(step.id),
    }
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=event.subtype,
        message=event.message,
        details=details,
    )


async def emit_onboarding_session_started_event(
    session: Session,
    *,
    coordinator: Assistant,
    medium: str,
) -> bool:
    """Notify Unity that the user just resolved the onboarding picker.

    Console fires this exactly once per picker resolution (chat or
    call). On the chat branch the event drives Unity's reactive
    handler, which pushes a notification and triggers an LLM run —
    Unity then either introduces itself (when the transcript is
    empty) or opens with a brief recap of progress (when prior
    Coordinator messages exist). On the call branch the event is
    informational: the actual call greeting is produced by the
    voice-agent's own sidecar LLM, which reads
    ``Coordinator/State`` (mode + derived progress) and the call's
    chat-history snapshot to pick between intro and recap. We still
    fire it on call so we have a single auditable signal of "the
    user just engaged the Coordinator" regardless of medium.

    ``completed_step_ids`` on the event details is the authoritative
    server-side derivation (:func:`derive_onboarding_progress`), so
    steps completed in earlier sessions — which never produce
    transition events — are still visible to Unity's opening turn.

    Gated on ``Coordinator/State.onboarding_active`` like the
    other onboarding events; emissions while inactive are
    silently dropped (returns ``False``).
    """
    if medium not in ONBOARDING_SESSION_MEDIUMS:
        logger.warning(
            "Ignoring unknown onboarding session medium: %s",
            medium,
        )
        return False
    details: dict[str, Any] = {"medium": medium}
    completed_step_ids = derive_onboarding_progress(session, coordinator=coordinator)
    skipped_step_ids = get_coordinator_state(session, coordinator=coordinator).get(
        "skipped_step_ids",
        [],
    )
    if completed_step_ids:
        details["completed_step_ids"] = completed_step_ids
    if skipped_step_ids:
        details["skipped_step_ids"] = normalize_onboarding_step_ids(skipped_step_ids)
    message = (
        "User just opened the onboarding chat with you — "
        "respond with one short opening turn."
        if medium == ONBOARDING_SESSION_MEDIUM_CHAT
        else "User just started an onboarding voice call with you."
    )
    return await notify_coordinator_onboarding_event(
        session,
        coordinator=coordinator,
        subtype=SUBTYPE_ONBOARDING_SESSION_STARTED,
        message=message,
        details=details,
    )
