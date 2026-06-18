"""Organization-wide shared team lifecycle and enrollment."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from orchestra.db.dao.team_dao import TeamDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    Organization,
    OrganizationMember,
    Team,
)
from orchestra.services.contact_membership_service import (
    ensure_team_contact_memberships,
)
from orchestra.services.coordinator_service import (
    ensure_workspace_coordinator_provisioned,
    get_workspace_coordinator,
)
from orchestra.services.team_cleanup_service import delete_team as run_team_cleanup
from orchestra.services.team_membership_refresh_service import (
    MembershipRefreshPayload,
    membership_refresh_payloads,
)

ORG_WIDE_SHARING_TEAM_NAME = "Org"
ORG_WIDE_SHARING_TEAM_DESCRIPTION = (
    "Organization-wide shared pool for knowledge, skills, and general know-how."
)


class OrgWideSharingConflictError(Exception):
    """Raised when the managed Org team cannot be created safely."""


class WorkspaceCoordinatorProvisioningError(Exception):
    """Raised when a member coordinator cannot be provisioned."""


@dataclass(slots=True)
class SharingEnrollmentResult:
    """Side effects produced while syncing sharing memberships."""

    created_coordinator_ids: list[int] = field(default_factory=list)
    refresh_payloads: list[MembershipRefreshPayload] = field(default_factory=list)

    def extend(self, other: "SharingEnrollmentResult") -> None:
        self.created_coordinator_ids.extend(other.created_coordinator_ids)
        self.refresh_payloads.extend(other.refresh_payloads)


async def add_coordinator_to_team(
    session: Session,
    *,
    team: Team,
    member_user_id: str,
    actor_user_id: str,
) -> tuple[Assistant, SharingEnrollmentResult]:
    """Provision a member workspace coordinator and add it to a team."""

    existing_workspace_coordinator = get_workspace_coordinator(
        session,
        user_id=member_user_id,
        organization_id=team.organization_id,
    )
    result = SharingEnrollmentResult()
    if existing_workspace_coordinator is not None:
        assistant = existing_workspace_coordinator
    else:
        try:
            assistant, created_workspace_coordinator = (
                await ensure_workspace_coordinator_provisioned(
                    session,
                    user_id=member_user_id,
                    organization_id=team.organization_id,
                )
            )
        except ValueError as exc:
            raise WorkspaceCoordinatorProvisioningError(
                "workspace_coordinator_provisioning_failed",
            ) from exc
        if created_workspace_coordinator:
            result.created_coordinator_ids.append(assistant.agent_id)

    team_dao = TeamDAO(session)
    if (
        team_dao.get_assistant_membership(
            team_id=team.id,
            assistant_id=assistant.agent_id,
        )
        is None
    ):
        team_dao.add_assistant_membership(
            team=team,
            assistant=assistant,
            added_by=actor_user_id,
        )
        ensure_team_contact_memberships(session, [(assistant.agent_id, team.id)])
        result.refresh_payloads.extend(
            membership_refresh_payloads(session, [assistant])
        )
    else:
        ensure_team_contact_memberships(session, [(assistant.agent_id, team.id)])

    return assistant, result


def add_assistant_to_team(
    session: Session,
    *,
    team: Team,
    assistant: Assistant,
    actor_user_id: str,
) -> SharingEnrollmentResult:
    """Add an assistant to a team with its team contact overlays."""

    result = SharingEnrollmentResult()
    team_dao = TeamDAO(session)
    if (
        team_dao.get_assistant_membership(
            team_id=team.id,
            assistant_id=assistant.agent_id,
        )
        is None
    ):
        team_dao.add_assistant_membership(
            team=team,
            assistant=assistant,
            added_by=actor_user_id,
        )
        ensure_team_contact_memberships(session, [(assistant.agent_id, team.id)])
        result.refresh_payloads.extend(
            membership_refresh_payloads(session, [assistant])
        )
    else:
        ensure_team_contact_memberships(session, [(assistant.agent_id, team.id)])
    return result


async def enroll_member_in_org_wide_team(
    session: Session,
    *,
    org: Organization,
    member_user_id: str,
    actor_user_id: str,
) -> SharingEnrollmentResult:
    """Enroll one organization member into the managed org-wide team."""

    team = _managed_team(session, org)
    if team is None:
        return SharingEnrollmentResult()

    team_dao = TeamDAO(session)
    if not team_dao.is_team_member(team.id, member_user_id):
        team_dao.add_member(team.id, member_user_id)

    _, result = await add_coordinator_to_team(
        session,
        team=team,
        member_user_id=member_user_id,
        actor_user_id=actor_user_id,
    )
    return result


def enroll_assistant_in_org_wide_team(
    session: Session,
    *,
    org: Organization,
    assistant: Assistant,
    actor_user_id: str,
) -> SharingEnrollmentResult:
    """Enroll one organization assistant into the managed org-wide team."""

    team = _managed_team(session, org)
    if team is None:
        return SharingEnrollmentResult()
    return add_assistant_to_team(
        session,
        team=team,
        assistant=assistant,
        actor_user_id=actor_user_id,
    )


async def enable_org_wide_sharing(
    session: Session,
    *,
    org: Organization,
    actor_user_id: str,
) -> SharingEnrollmentResult:
    """Create or repair the managed org-wide team and sync all memberships."""

    team_dao = TeamDAO(session)
    team = _managed_team(session, org)
    if team is None:
        existing = team_dao.get_by_name(ORG_WIDE_SHARING_TEAM_NAME, org.id)
        if existing is not None and not existing.is_org_wide_sharing:
            raise OrgWideSharingConflictError("org_team_name_reserved")
        team = existing or team_dao.create(
            name=ORG_WIDE_SHARING_TEAM_NAME,
            organization_id=org.id,
            description=ORG_WIDE_SHARING_TEAM_DESCRIPTION,
            is_org_wide_sharing=True,
        )
        team.is_org_wide_sharing = True
        org.org_wide_sharing_team_id = team.id

    org.org_wide_sharing_enabled = True
    return await sync_org_wide_sharing(session, org=org, actor_user_id=actor_user_id)


async def disable_org_wide_sharing(
    session: Session,
    *,
    org: Organization,
    actor_user_id: str,
) -> None:
    """Delete the managed org-wide team and disable org-wide sharing."""

    team_id = org.org_wide_sharing_team_id
    if team_id is None:
        org.org_wide_sharing_enabled = False
        session.flush()
        session.commit()
        return

    await run_team_cleanup(
        session,
        team_id=team_id,
        user_id=actor_user_id,
        organization_id=org.id,
    )
    refreshed_org = session.get(Organization, org.id)
    if refreshed_org is not None:
        refreshed_org.org_wide_sharing_enabled = False
        refreshed_org.org_wide_sharing_team_id = None
        session.commit()


async def sync_org_wide_sharing(
    session: Session,
    *,
    org: Organization,
    actor_user_id: str,
) -> SharingEnrollmentResult:
    """Reconcile the managed org team with current members and assistants."""

    team = _managed_team(session, org)
    if team is None:
        return SharingEnrollmentResult()

    result = SharingEnrollmentResult()
    members = (
        session.query(OrganizationMember)
        .filter(OrganizationMember.organization_id == org.id)
        .order_by(OrganizationMember.id.asc())
        .all()
    )
    for member in members:
        result.extend(
            await enroll_member_in_org_wide_team(
                session,
                org=org,
                member_user_id=member.user_id,
                actor_user_id=actor_user_id,
            ),
        )

    assistants = (
        session.query(Assistant)
        .filter(
            Assistant.organization_id == org.id,
            Assistant.is_coordinator.is_(False),
        )
        .order_by(Assistant.agent_id.asc())
        .all()
    )
    for assistant in assistants:
        result.extend(
            add_assistant_to_team(
                session,
                team=team,
                assistant=assistant,
                actor_user_id=actor_user_id,
            ),
        )
    return result


def _managed_team(session: Session, org: Organization) -> Team | None:
    if org.org_wide_sharing_team_id is None:
        return None
    team = session.get(Team, org.org_wide_sharing_team_id)
    if team is None:
        return None
    if team.organization_id != org.id or not team.is_org_wide_sharing:
        return None
    return team
