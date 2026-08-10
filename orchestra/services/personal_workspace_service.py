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
UNIFY_STAFF_EMAIL_DOMAIN = "@unify.ai"


def user_is_unify_staff(session: Session, user_id: str) -> bool:
    """Whether the user holds a verified unify.ai mailbox.

    The email is trustworthy as an identity signal: self-serve signup
    creates no ``User`` row until the address is verified, and
    admin-created users are deliberate. Org *names* are not — "Unify" is
    claimable by anyone wherever the platform's own org does not exist
    (fresh stacks, self-host), so anything granting internal privileges
    must check the mailbox, not just membership in an org so named.
    """
    email = session.query(User.email).filter(User.id == user_id).scalar()
    return bool(email) and email.lower().endswith(UNIFY_STAFF_EMAIL_DOMAIN)


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
    """Whether the user is Unify staff operating inside the Unify org.

    Membership alone is spoofable (see :func:`user_is_unify_staff`), so
    it only counts for a verified unify.ai mailbox.
    """
    if not user_is_unify_staff(session, user_id):
        return False
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
    query = (
        session.query(OrganizationMember)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .filter(OrganizationMember.user_id == user_id)
    )
    # For a non-staff user an org named "Unify" is just an org — only
    # staff get the carve-out for the platform's own workspace.
    if user_is_unify_staff(session, user_id):
        query = query.filter(Organization.name != UNIFY_ORGANIZATION_NAME)
    return query.first() is not None


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


def reenable_personal_workspace_if_no_org(session: Session, user_id: str) -> bool:
    """Re-enable a user's personal workspace once they no longer belong to any
    (non-Unify) organization.

    ``disable_personal_workspace_for_org_member`` is the counterpart that turns
    the flag on when a user joins/creates a customer org. Membership is
    reversible (a user can leave an org, or the org can be deleted), so the flag
    must be cleared again — otherwise personal-context billing (e.g.
    ``get_billing_entity``/``credits/deduct``) stays permanently blocked for a
    user who is no longer an org member. Call this after the membership has been
    removed. Returns ``True`` if the workspace was re-enabled.
    """

    if user_has_non_unify_membership(session, user_id):
        return False

    user = session.query(User).filter(User.id == user_id).first()
    if user is None or user.personal_workspace_disabled_at is None:
        return False

    user.personal_workspace_disabled_at = None
    user.personal_workspace_disabled_reason = None
    user.personal_workspace_disabled_org_id = None
    return True
