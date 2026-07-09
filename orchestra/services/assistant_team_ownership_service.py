"""Transfer an existing org assistant to team-owned scope.

The context/log mechanics — collision reconciliation, populated-table
merging, rename, FK re-rooting, ownership rebrand — are the generic tree
operations in :mod:`orchestra.services.context_merge_service`; this service
adds the assistant-specific semantics (validation, contact overlays, team
membership) and the transfer API's error vocabulary.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    ContactMembership,
    Team,
)
from orchestra.db.scope import OwnerScope, owner_key
from orchestra.services.contact_membership_service import (
    ensure_team_contact_memberships,
)
from orchestra.services.context_merge_service import (
    ContextMergeError,
    merge_context_trees,
    update_tree_ownership,
)
from orchestra.services.org_wide_sharing_service import add_assistant_to_team
from orchestra.services.team_membership_refresh_service import (
    membership_refresh_payloads,
    publish_membership_refreshes_best_effort,
)

ASSISTANTS_PROJECT_NAME = "Assistants"

# Generic tree-merge error codes -> this API's detail vocabulary.
_MERGE_ERROR_DETAILS = {
    "collision_both_have_data": "team_memory_collision_both_have_data",
    "schema_mismatch": "team_memory_merge_schema_mismatch",
    "secret_conflict": "team_memory_merge_secret_conflict",
    "function_conflict": "team_memory_merge_function_conflict",
    "unique_key_conflict": "team_memory_merge_unique_key_conflict",
    "versioned_context": "team_memory_merge_versioned_context",
    "collision_unresolved": "team_memory_collision_unresolved",
    "source_not_drained": "team_memory_transfer_incomplete",
}


class TeamOwnershipTransferError(Exception):
    """Raised when an assistant cannot be converted to team-owned."""


def _transfer_detail(exc: ContextMergeError) -> str:
    detail = _MERGE_ERROR_DETAILS[exc.code]
    if exc.subject and exc.code != "source_not_drained":
        return f"{detail}: {exc.subject}"
    return detail


async def transfer_assistant_to_team_owned(
    session: Session,
    *,
    assistant_id: int,
    owner_team_id: int,
    actor_user_id: str,
    merge_memory: bool = False,
) -> dict[str, object]:
    """Convert an org assistant from personal memory to team-owned scope.

    Contexts under ``{user_id}/{agent_id}/...`` move to ``Teams/{team_id}/...``
    following the same shared-root convention team-owned assistants use at hire
    time. Personal contact overlays are removed; the owning team becomes the
    assistant's only memory root.

    When the team tree already holds data for a table the assistant also has
    data in, the transfer refuses by default; with ``merge_memory`` the
    assistant's rows are merged into the team table (key values re-numbered
    above the team's).
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

    personal_prefix = f"{assistant.user_id}/{assistant_id}"
    team_prefix = f"Teams/{owner_team_id}"

    try:
        merge_result = merge_context_trees(
            session,
            context_dao,
            project_id=project_id,
            source_prefix=personal_prefix,
            target_prefix=team_prefix,
            merge_populated=merge_memory,
        )
    except ContextMergeError as exc:
        raise TeamOwnershipTransferError(_transfer_detail(exc)) from exc

    update_tree_ownership(
        session,
        project_id=project_id,
        prefix=team_prefix,
        owner_scope=OwnerScope.TEAM,
        owner_id=owner_team_id,
        previous_owner_key=owner_key(OwnerScope.ASSISTANT, assistant_id),
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
        "contexts_renamed": merge_result.contexts_renamed,
        "contexts_merged": merge_result.contexts_merged,
        "duplicate_contacts": merge_result.duplicate_contacts,
        "memory_root": team_prefix,
    }
