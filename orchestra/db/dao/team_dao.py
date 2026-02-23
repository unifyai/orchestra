"""Data Access Object for Team model."""

from typing import List, Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Team, TeamMember


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
    ) -> None:
        """
        Update a team.

        :param id: Team ID.
        :param name: New team name.
        :param description: New team description.
        """
        team = self.get(id)
        if not team:
            return

        if name is not None:
            team.name = name
        if description is not None:
            team.description = description

        self.session.flush()

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
