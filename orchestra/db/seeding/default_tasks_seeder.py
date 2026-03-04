"""
Module for seeding default tasks-related data for users.
"""

from typing import Dict

from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.models.orchestra_models import Interface, Project, Tab, Tile


class DefaultTasksSeeder:
    """
    Seeder class for creating default Unity project, interface, tab, and table tile
    for tasks management.
    """

    @staticmethod
    def seed(session: Session, user_id: str) -> Dict[str, str]:
        """
        Seeds a default Unity project, interface, tab, and table tile for the given user.

        Args:
            session: The database session
            user_id: The ID of the user to seed default tasks for

        Returns:
            Dictionary containing the IDs of created/fetched entities
        """
        organization_member_dao = OrganizationMemberDAO(session)
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, organization_member_dao, context_dao)
        interface_dao = InterfaceDAO(session)
        tab_dao = TabDAO(session)
        tile_dao = TileDAO(session)

        # Step 1: Fetch or create Unity project
        project = DefaultTasksSeeder._get_or_create_project(project_dao, user_id)

        # Step 2: Fetch or create Unity interface
        interface = DefaultTasksSeeder._get_or_create_interface(
            interface_dao,
            project.id,
        )

        # Step 3: Fetch or create Tasks tab
        tab = DefaultTasksSeeder._get_or_create_tab(tab_dao, interface.id)

        # Step 4: Fetch or create Tasks table tile
        tile = DefaultTasksSeeder._get_or_create_table_tile(tile_dao, tab.id)

        # Flush to persist within the current transaction (caller manages commit)
        session.flush()

        # Return the IDs of all created/fetched entities
        return {
            "project_id": project.id,
            "interface_id": interface.id,
            "tab_id": tab.id,
            "tile_id": tile.id,
        }

    @staticmethod
    def _get_or_create_project(project_dao: ProjectDAO, user_id: str) -> Project:
        """
        Gets or creates a Unity project for the given user.

        Args:
            project_dao: The ProjectDAO instance
            user_id: The user ID

        Returns:
            The Project object
        """
        # Try to fetch existing Unity project for this user
        project = project_dao.get_by_user_and_name(user_id=user_id, name="Unity")

        # If project doesn't exist, create it
        if not project:
            project_dao.create(user_id=user_id, name="Unity")
            project = project_dao.get_by_user_and_name(user_id=user_id, name="Unity")

        return project

    @staticmethod
    def _get_or_create_interface(
        interface_dao: InterfaceDAO,
        project_id: int,
    ) -> Interface:
        """
        Gets or creates a Unity interface for the given project.

        Args:
            interface_dao: The InterfaceDAO instance
            project_id: The project ID

        Returns:
            The Interface object
        """
        # Try to fetch existing Unity interface for this project
        interface = interface_dao.get_by_project_and_name(
            project_id=project_id,
            name="Unity",
        )

        # If interface doesn't exist, create it
        if not interface:
            interface = interface_dao.create_interface(
                project_id=project_id,
                name="Unity",
            )

        return interface

    @staticmethod
    def _get_or_create_tab(tab_dao: TabDAO, interface_id: str) -> Tab:
        """
        Gets or creates a Tasks tab for the given interface.

        Args:
            tab_dao: The TabDAO instance
            interface_id: The interface ID

        Returns:
            The Tab object
        """
        # Try to fetch existing Tasks tab for this interface
        tab = tab_dao.get_by_interface_and_name(interface_id=interface_id, name="Tasks")

        # If tab doesn't exist, create it
        if not tab:
            tab = tab_dao.create_tab(interface_id=interface_id, name="Tasks")

        return tab

    @staticmethod
    def _get_or_create_table_tile(tile_dao: TileDAO, tab_id: str) -> Tile:
        """
        Gets or creates a Tasks table tile for the given tab.

        Args:
            tile_dao: The TileDAO instance
            tab_id: The tab ID

        Returns:
            The Tile object
        """
        # Try to fetch existing table tile for this tab with context="Tasks" and table="Tasks"
        tile = tile_dao.get_by_tab_and_name(tab_id=tab_id, name="Tasks")

        # If tile doesn't exist, create it
        if not tile:
            tile = tile_dao.create_tile(
                tab_id=tab_id,
                name="Tasks",
                context="Tasks",
                type="Table",
            )

        return tile
