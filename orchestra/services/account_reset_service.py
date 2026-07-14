from __future__ import annotations

"""Rewind a user's personal workspace to its fresh-signup state.

Powers the staging-only "Reset account" control. It tears down every personal
assistant (hired teammates *and* the Coordinator), clears the user's non-email
contact details, re-provisions a pristine Coordinator through the exact signup
path, and rewinds onboarding -- leaving the account looking as it did moments
after signup. Organization memberships, org-scoped Coordinators, and billing are
intentionally left untouched.

Hired assistants go through the same teardown pipeline as an individual delete
(runtime shutdown, contact deprovisioning, GCS + Pub/Sub cleanup). The
Coordinator is handled differently on purpose: its universal contacts point at
*shared* platform pool resources (e.g. the Unity Twilio numbers used by every
Coordinator), so we must never run the contact-releasing deprovision against it.
Deleting the Coordinator row simply drops its own ``assistant_contacts``
associations via ``ON DELETE CASCADE`` without releasing the shared number, and
the follow-up cleanup task only reclaims the abandoned agent's runtime/topic.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.models.orchestra_models import User
from orchestra.services.assistant_cleanup_service import (
    CleanupSource,
    build_cleanup_spec_from_assistant,
    build_cleanup_specs_for_assistants,
    deprovision_assistant_contacts,
    enqueue_cleanup_tasks,
    purge_assistant_owner,
)
from orchestra.services.coordinator_service import (
    ensure_personal_coordinator_provisioned,
)
from orchestra.services.team_cleanup_service import purge_assistant_memberships

logger = logging.getLogger(__name__)


@dataclass
class AccountResetResult:
    """Outcome of a personal-account reset."""

    deleted_assistant_ids: list[int] = field(default_factory=list)
    new_coordinator_id: int | None = None
    cleanup_task_ids: list[int] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)


async def reset_personal_account(
    session: Session,
    *,
    user_id: str,
) -> AccountResetResult:
    """Reset a user's personal workspace to the state of a brand-new signup.

    Commits once at the end so the teardown, contact wipe, onboarding rewind, and
    fresh Coordinator provisioning all land atomically.
    """
    dao = AssistantDAO(session)
    result = AccountResetResult()

    personal_assistants = dao.list_assistants_for_user(
        user_id,
        organization_id=None,
    )
    hired = [a for a in personal_assistants if not a.is_coordinator]
    coordinators = [a for a in personal_assistants if a.is_coordinator]

    # Hired assistants: full delete pipeline, releasing their dedicated
    # (per-assistant) contact resources exactly like an "End contract".
    hired_specs = build_cleanup_specs_for_assistants(session, hired)
    if hired_specs:
        contact_result = await deprovision_assistant_contacts(
            session,
            hired_specs,
            soft_delete_successes=True,
        )
        result.cleanup_errors.extend(contact_result["errors"])

    # Coordinators: never run contact deprovision (shared pool resources). The
    # empty-contact spec still reclaims the abandoned agent's runtime, Pub/Sub
    # topic, and GCS footprint once the row is gone.
    coordinator_specs = [
        build_cleanup_spec_from_assistant(coordinator, contacts=[])
        for coordinator in coordinators
    ]

    result.cleanup_task_ids = [
        task.id
        for task in enqueue_cleanup_tasks(
            session,
            [*hired_specs, *coordinator_specs],
            source_flow=CleanupSource.ACCOUNT_RESET,
        )
    ]

    for assistant in personal_assistants:
        await purge_assistant_memberships(session, assistant=assistant)

    for assistant in personal_assistants:
        assistant_id = int(assistant.agent_id)
        result.deleted_assistant_ids.append(assistant_id)
        session.delete(assistant)
        purge_assistant_owner(
            session,
            assistant_id=assistant_id,
            user_id=user_id,
            organization_id=None,
        )

    # Emit the row deletes before provisioning the replacement Coordinator so the
    # partial-unique "one personal coordinator per user" index never sees two.
    session.flush()

    _clear_user_contact_details(session, user_id=user_id)
    OnboardingStatusDAO(session).reset(user_id)

    coordinator, _ = await ensure_personal_coordinator_provisioned(
        session,
        user_id=user_id,
    )
    result.new_coordinator_id = int(coordinator.agent_id)

    session.commit()
    logger.info(
        "Reset personal account for user %s: deleted=%s new_coordinator=%s",
        user_id,
        result.deleted_assistant_ids,
        result.new_coordinator_id,
    )
    return result


def _clear_user_contact_details(session: Session, *, user_id: str) -> None:
    """Drop the user's contact details beyond their email (phone/WhatsApp/Discord)."""
    user = session.get(User, user_id)
    if user is None:
        return
    user.phone_number = None
    user.whatsapp_number = None
    user.discord_id = None
