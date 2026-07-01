from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrganizationMember,
    User,
)
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    build_cleanup_specs_for_assistants,
    enqueue_cleanup_tasks,
)

PERSONAL_WORKSPACE_DISABLED_REASON_ORG_MEMBER = "organization_membership"
UNIFY_ORGANIZATION_NAME = "Unify"


@dataclass(frozen=True)
class PersonalWorkspaceDisableResult:
    user_id: str
    disabled: bool
    personal_assistants: int = 0
    contacts_soft_deleted: int = 0
    cleanup_tasks_queued: int = 0
    cleanup_task_ids: tuple[int, ...] = ()
    exempt_unify_member: bool = False


def user_is_unify_member(session: Session, user_id: str) -> bool:
    return (
        session.query(OrganizationMember)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .filter(
            OrganizationMember.user_id == user_id,
            Organization.name == UNIFY_ORGANIZATION_NAME,
        )
        .first()
        is not None
    )


def user_has_non_unify_membership(session: Session, user_id: str) -> bool:
    return (
        session.query(OrganizationMember)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .filter(
            OrganizationMember.user_id == user_id,
            Organization.name != UNIFY_ORGANIZATION_NAME,
        )
        .first()
        is not None
    )


def personal_workspace_is_disabled(session: Session, user_id: str) -> bool:
    user = session.query(User).filter(User.id == user_id).first()
    return bool(user and user.personal_workspace_disabled_at is not None)


def ensure_personal_workspace_allowed(session: Session, user_id: str) -> None:
    if personal_workspace_is_disabled(session, user_id):
        raise ValueError("Personal workspace is disabled for organization members.")


def disable_personal_workspace_for_org_member(
    session: Session,
    user_id: str,
    organization_id: int,
) -> PersonalWorkspaceDisableResult:
    """Disable a user's personal workspace after joining a customer organization."""

    if user_is_unify_member(session, user_id):
        return PersonalWorkspaceDisableResult(
            user_id=user_id,
            disabled=False,
            exempt_unify_member=True,
        )

    user = session.query(User).filter(User.id == user_id).first()
    if user is None:
        return PersonalWorkspaceDisableResult(user_id=user_id, disabled=False)

    now = datetime.now(timezone.utc)
    user.personal_workspace_disabled_at = user.personal_workspace_disabled_at or now
    user.personal_workspace_disabled_reason = (
        PERSONAL_WORKSPACE_DISABLED_REASON_ORG_MEMBER
    )
    user.personal_workspace_disabled_org_id = organization_id

    personal_assistants = (
        session.query(Assistant)
        .filter(
            Assistant.user_id == user_id,
            Assistant.organization_id.is_(None),
        )
        .all()
    )
    cleanup_specs = build_cleanup_specs_for_assistants(session, personal_assistants)

    cleanup_tasks = enqueue_cleanup_tasks(
        session,
        cleanup_specs,
        source_flow=CleanupSource.PERSONAL_WORKSPACE_DISABLED,
    )

    contact_dao = AssistantContactDAO(session)
    contacts_soft_deleted = 0
    for assistant in personal_assistants:
        contacts_soft_deleted += len(
            contact_dao.soft_delete_all_contacts_for_assistant(int(assistant.agent_id)),
        )

    return PersonalWorkspaceDisableResult(
        user_id=user_id,
        disabled=True,
        personal_assistants=len(personal_assistants),
        contacts_soft_deleted=contacts_soft_deleted,
        cleanup_tasks_queued=len(cleanup_tasks),
        cleanup_task_ids=tuple(int(task.id) for task in cleanup_tasks),
    )
