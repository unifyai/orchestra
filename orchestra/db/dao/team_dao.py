"""Data Access Object for Team model."""

from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    TEAM_STATUS_ACTIVE,
    Assistant,
    Team,
    TeamAssistantMembership,
    TeamMember,
)

TEAM_STATUS_DELETING = "deleting"


class TeamDAO:
    """DAO for managing teams."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        name: str,
        organization_id: int,
        description: Optional[str] = None,
    ) -> Team:
        """
        Create a new team.

        :param name: Team name.
        :param organization_id: Organization ID.
        :param description: Team description.
        :return: The created Team object.
        """
        team = Team(
            name=name,
            organization_id=organization_id,
            description=description,
        )
        self.session.add(team)
        self.session.flush()
        return team

    def get(self, id: int) -> Optional[Team]:
        """
        Get a team by ID.

        :param id: Team ID.
        :return: Team object or None if not found.
        """
        return self.session.query(Team).filter_by(id=id).first()

    def get_by_name(
        self,
        name: str,
        organization_id: int,
    ) -> Optional[Team]:
        """
        Get a team by name and organization.

        :param name: Team name.
        :param organization_id: Organization ID.
        :return: Team object or None if not found.
        """
        return (
            self.session.query(Team)
            .filter_by(name=name, organization_id=organization_id)
            .first()
        )

    def list_organization_teams(self, organization_id: int) -> List[Team]:
        """
        Get all teams for an organization.

        :param organization_id: Organization ID.
        :return: List of Team objects.
        """
        return self.session.query(Team).filter_by(organization_id=organization_id).all()

    def update(
        self,
        id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Team | None:
        """
        Update a team.

        :param id: Team ID.
        :param name: New team name.
        :param description: New team description.
        :return: Updated team row, or None when the team does not exist.
        """
        team = self.get(id)
        if not team:
            return None

        if name is not None:
            team.name = name
        if description is not None:
            team.description = description

        self.session.flush()
        return team

    def delete(self, id: int) -> None:
        """
        Delete a team.

        :param id: Team ID.
        """
        team = self.get(id)
        if team:
            self.session.delete(team)
            self.session.flush()

    def add_member(self, team_id: int, user_id: str) -> TeamMember:
        """
        Add a user to a team.

        :param team_id: Team ID.
        :param user_id: User ID.
        :return: The created TeamMember object.
        """
        team_member = TeamMember(team_id=team_id, user_id=user_id)
        self.session.add(team_member)
        self.session.flush()
        return team_member

    def remove_member(self, team_id: int, user_id: str) -> None:
        """
        Remove a user from a team.

        :param team_id: Team ID.
        :param user_id: User ID.
        """
        team_member = (
            self.session.query(TeamMember)
            .filter_by(team_id=team_id, user_id=user_id)
            .first()
        )
        if team_member:
            self.session.delete(team_member)
            self.session.flush()

    def get_team_members(self, team_id: int) -> List[str]:
        """
        Get all user IDs in a team.

        :param team_id: Team ID.
        :return: List of user IDs.
        """
        members = self.session.query(TeamMember).filter_by(team_id=team_id).all()
        return [member.user_id for member in members]

    def get_user_teams(self, user_id: str, organization_id: int) -> List[Team]:
        """
        Get all teams a user belongs to in an organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :return: List of Team objects.
        """
        result = (
            self.session.query(Team)
            .join(TeamMember, Team.id == TeamMember.team_id)
            .filter(
                TeamMember.user_id == user_id,
                Team.organization_id == organization_id,
            )
            .all()
        )
        return result

    def is_team_member(self, team_id: int, user_id: str) -> bool:
        """
        Check if a user is a member of a team.

        :param team_id: Team ID.
        :param user_id: User ID.
        :return: True if user is a member, False otherwise.
        """
        team_member = (
            self.session.query(TeamMember)
            .filter_by(team_id=team_id, user_id=user_id)
            .first()
        )
        return team_member is not None

    def remove_user_from_all_org_teams(
        self,
        user_id: str,
        organization_id: int,
    ) -> int:
        """
        Remove a user from all teams in an organization.
        Called when a member is removed from an organization.

        :param user_id: User ID.
        :param organization_id: Organization ID.
        :returns: Count of team memberships removed.
        """
        # Get all team IDs in this org
        org_team_ids = [t.id for t in self.list_organization_teams(organization_id)]

        if not org_team_ids:
            return 0

        deleted = (
            self.session.query(TeamMember)
            .filter(
                TeamMember.user_id == user_id,
                TeamMember.team_id.in_(org_team_ids),
            )
            .delete(synchronize_session=False)
        )

        self.session.flush()
        return deleted

    def get_assistant(self, assistant_id: int) -> Optional[Assistant]:
        """Return an assistant by primary key."""

        return self.session.get(Assistant, assistant_id)

    def get_assistant_membership(
        self,
        *,
        team_id: int,
        assistant_id: int,
    ) -> Optional[TeamAssistantMembership]:
        """Return a live assistant membership for a team pair."""

        return self.session.get(
            TeamAssistantMembership,
            {"team_id": team_id, "assistant_id": assistant_id},
        )

    def add_assistant_membership(
        self,
        *,
        team: Team,
        assistant: Assistant,
        added_by: str,
    ) -> TeamAssistantMembership:
        """Materialize a live assistant membership in a team."""

        membership = TeamAssistantMembership(
            team_id=team.id,
            assistant_id=assistant.agent_id,
            added_by=added_by,
        )
        self.session.add(membership)
        self.session.flush()
        return membership

    def list_assistant_members(
        self,
        team_id: int,
    ) -> list[tuple[TeamAssistantMembership, Assistant]]:
        """Return live assistant members for a team."""

        rows = (
            self.session.query(TeamAssistantMembership, Assistant)
            .join(
                Assistant,
                Assistant.agent_id == TeamAssistantMembership.assistant_id,
            )
            .filter(TeamAssistantMembership.team_id == team_id)
            .order_by(TeamAssistantMembership.created_at.asc())
            .all()
        )
        return list(rows)

    def list_teams_for_assistant(self, assistant_id: int) -> list[Team]:
        """Return active teams where an assistant is a live member."""

        return list(
            self.session.query(Team)
            .join(
                TeamAssistantMembership,
                TeamAssistantMembership.team_id == Team.id,
            )
            .filter(
                TeamAssistantMembership.assistant_id == assistant_id,
                Team.status == TEAM_STATUS_ACTIVE,
            )
            .order_by(Team.id.asc())
            .all(),
        )

    def team_ids_for_assistant(self, assistant_id: int) -> list[int]:
        """Return sorted live team ids for an assistant."""

        return self.team_ids_for_assistants([assistant_id]).get(assistant_id, [])

    def team_ids_for_assistants(
        self,
        assistant_ids: Iterable[int],
    ) -> dict[int, list[int]]:
        """Return sorted live team ids keyed by assistant id."""

        ids = list(assistant_ids)
        if not ids:
            return {}
        rows = (
            self.session.query(
                TeamAssistantMembership.assistant_id,
                Team.id,
            )
            .join(Team, Team.id == TeamAssistantMembership.team_id)
            .filter(TeamAssistantMembership.assistant_id.in_(ids))
            .filter(Team.status == TEAM_STATUS_ACTIVE)
            .order_by(
                TeamAssistantMembership.assistant_id.asc(),
                Team.id.asc(),
            )
            .all()
        )
        memberships: dict[int, list[int]] = {assistant_id: [] for assistant_id in ids}
        for assistant_id, team_id in rows:
            memberships.setdefault(int(assistant_id), []).append(int(team_id))
        return memberships

    def team_summaries_for_assistant(
        self,
        assistant_id: int,
    ) -> list[dict[str, int | str | None]]:
        """Return sorted live team summaries for an assistant."""

        return self.team_summaries_for_assistants([assistant_id]).get(
            assistant_id,
            [],
        )

    def team_summaries_for_assistants(
        self,
        assistant_ids: Iterable[int],
    ) -> dict[int, list[dict[str, int | str | None]]]:
        """Return sorted live team summaries keyed by assistant id."""

        ids = list(assistant_ids)
        if not ids:
            return {}
        rows = (
            self.session.query(
                TeamAssistantMembership.assistant_id,
                Team.id,
                Team.name,
                Team.description,
            )
            .join(Team, Team.id == TeamAssistantMembership.team_id)
            .filter(TeamAssistantMembership.assistant_id.in_(ids))
            .filter(Team.status == TEAM_STATUS_ACTIVE)
            .order_by(
                TeamAssistantMembership.assistant_id.asc(),
                Team.id.asc(),
            )
            .all()
        )
        memberships: dict[int, list[dict[str, int | str | None]]] = {
            assistant_id: [] for assistant_id in ids
        }
        for assistant_id, team_id, name, description in rows:
            memberships.setdefault(int(assistant_id), []).append(
                {
                    "team_id": int(team_id),
                    "name": str(name),
                    "description": description,
                },
            )
        return memberships

    def assistant_team_ids_for_user(
        self,
        *,
        user_id: str,
        organization_id: int,
    ) -> list[int]:
        """Return team ids where the user's workspace coordinator is a member."""

        coordinator = (
            self.session.query(Assistant)
            .filter(
                Assistant.user_id == user_id,
                Assistant.organization_id == organization_id,
                Assistant.is_coordinator.is_(True),
            )
            .order_by(Assistant.agent_id.asc())
            .first()
        )
        if coordinator is None:
            return []
        return self.team_ids_for_assistant(coordinator.agent_id)

    def remove_assistant_from_org_teams(
        self,
        *,
        assistant_id: int,
        organization_id: int,
    ) -> list[int]:
        """Remove an assistant from every team in an organization."""

        team_ids = [
            int(team_id)
            for (team_id,) in self.session.query(TeamAssistantMembership.team_id)
            .join(Team, Team.id == TeamAssistantMembership.team_id)
            .filter(
                TeamAssistantMembership.assistant_id == assistant_id,
                Team.organization_id == organization_id,
            )
            .all()
        ]
        if not team_ids:
            return []

        self.session.query(TeamAssistantMembership).filter(
            TeamAssistantMembership.assistant_id == assistant_id,
            TeamAssistantMembership.team_id.in_(team_ids),
        ).delete(synchronize_session=False)
        self.session.flush()
        return team_ids
