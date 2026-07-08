"""Transfer an existing org assistant to team-owned scope."""

from __future__ import annotations

from sqlalchemy import or_, text, update
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    ContactMembership,
    Team,
)
from orchestra.db.scope import OwnerScope, owner_key
from orchestra.services.contact_membership_service import (
    ensure_team_contact_memberships,
)
from orchestra.services.org_wide_sharing_service import add_assistant_to_team
from orchestra.services.team_membership_refresh_service import (
    membership_refresh_payloads,
    publish_membership_refreshes_best_effort,
)

ASSISTANTS_PROJECT_NAME = "Assistants"


class TeamOwnershipTransferError(Exception):
    """Raised when an assistant cannot be converted to team-owned."""


async def transfer_assistant_to_team_owned(
    session: Session,
    *,
    assistant_id: int,
    owner_team_id: int,
    actor_user_id: str,
) -> dict[str, object]:
    """Convert an org assistant from personal memory to team-owned scope.

    Contexts under ``{user_id}/{agent_id}/...`` move to ``Teams/{team_id}/...``
    following the same shared-root convention team-owned assistants use at hire
    time. Personal contact overlays are removed; the owning team becomes the
    assistant's only memory root.
    """
    assistant_dao = AssistantDAO(session)
    assistant = assistant_dao.get_assistant_by_agent_id(assistant_id)
    if assistant is None:
        raise TeamOwnershipTransferError("assistant_not_found")
    if assistant.organization_id is None:
        raise TeamOwnershipTransferError("assistant_not_in_organization")
    if assistant.owner_team_id is not None:
        raise TeamOwnershipTransferError("assistant_already_team_owned")
    if assistant.is_coordinator:
        raise TeamOwnershipTransferError("coordinator_cannot_be_team_owned")

    team = session.get(Team, owner_team_id)
    if team is None or team.organization_id != assistant.organization_id:
        raise TeamOwnershipTransferError("team_not_in_organization")

    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    org_projects = project_dao.filter(
        organization_id=assistant.organization_id,
        name=ASSISTANTS_PROJECT_NAME,
    )
    if not org_projects:
        raise TeamOwnershipTransferError("assistants_project_missing")
    assistants_project = org_projects[0][0]
    project_id = assistants_project.id

    old_prefix = f"{assistant.user_id}/{assistant_id}"
    new_prefix = f"Teams/{owner_team_id}"
    renamed_count = context_dao.rename_with_children(
        project_id,
        old_prefix,
        new_prefix,
    )

    session.execute(
        update(Context)
        .where(
            Context.project_id == project_id,
            or_(
                Context.name == new_prefix,
                Context.name.like(f"{new_prefix}/%"),
            ),
        )
        .values(
            owner_scope=OwnerScope.TEAM.value,
            owner_id=owner_team_id,
        ),
    )
    session.flush()

    new_owner_key = owner_key(OwnerScope.TEAM, owner_team_id)
    old_owner_key = owner_key(OwnerScope.ASSISTANT, assistant_id)
    session.execute(
        text(
            """
            UPDATE log_event le
            SET owner_key = :new_owner_key
            FROM log_event_context lec
            JOIN context c ON c.id = lec.context_id
            WHERE le.id = lec.log_event_id
              AND le.project_id = :project_id
              AND lec.project_id = :project_id
              AND c.project_id = :project_id
              AND (
                    c.name = :new_prefix
                    OR c.name LIKE :new_prefix_like
              )
              AND le.owner_key = :old_owner_key
            """,
        ),
        {
            "project_id": project_id,
            "new_prefix": new_prefix,
            "new_prefix_like": f"{new_prefix}/%",
            "new_owner_key": new_owner_key,
            "old_owner_key": old_owner_key,
        },
    )
    session.execute(
        text(
            """
            UPDATE log_event_context lec
            SET owner_key = :new_owner_key
            FROM context c
            WHERE c.id = lec.context_id
              AND lec.project_id = :project_id
              AND c.project_id = :project_id
              AND (
                    c.name = :new_prefix
                    OR c.name LIKE :new_prefix_like
              )
              AND lec.owner_key = :old_owner_key
            """,
        ),
        {
            "project_id": project_id,
            "new_prefix": new_prefix,
            "new_prefix_like": f"{new_prefix}/%",
            "new_owner_key": new_owner_key,
            "old_owner_key": old_owner_key,
        },
    )

    session.query(ContactMembership).filter(
        ContactMembership.assistant_id == assistant_id,
        ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    ).delete(synchronize_session=False)

    assistant.owner_team_id = owner_team_id
    session.add(assistant)

    team_dao = TeamDAO(session)
    if (
        team_dao.get_assistant_membership(
            team_id=owner_team_id,
            assistant_id=assistant_id,
        )
        is None
    ):
        add_assistant_to_team(
            session,
            team=team,
            assistant=assistant,
            actor_user_id=actor_user_id,
        )
    else:
        ensure_team_contact_memberships(session, [(assistant_id, owner_team_id)])

    session.commit()
    refresh_payloads = membership_refresh_payloads(session, [assistant])
    await publish_membership_refreshes_best_effort(refresh_payloads)

    return {
        "agent_id": assistant_id,
        "owner_team_id": owner_team_id,
        "contexts_renamed": renamed_count,
        "memory_root": new_prefix,
    }
