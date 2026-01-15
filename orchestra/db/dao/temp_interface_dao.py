"""Data Access Object for temp_interface table (autosave/checkpoint functionality)."""

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import TempInterface


class TempInterfaceDAO:
    """Class for accessing temp_interface table."""

    def __init__(self, session: Session):
        """Initialize TempInterfaceDAO with a database session."""
        self.session = session

    def create(
        self,
        id: str,
        items: str,
        user_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        new_counter: Optional[int] = None,
        project: Optional[str] = None,
        context: Optional[str] = None,
        column_context: Optional[str] = None,
        color: Optional[str] = None,
    ) -> TempInterface:
        """
        Create a new temp interface.

        :param id: Unique identifier
        :param items: JSON string of interface items
        :param user_id: User who created this (optional)
        :param organization_id: Organization this belongs to (optional)
        :param new_counter: Counter for new items
        :param project: Project identifier
        :param context: Interface context
        :param column_context: Column context
        :param color: Interface color
        :return: Created TempInterface instance
        """
        temp_interface = TempInterface(
            id=id,
            items=items,
            user_id=user_id,
            organization_id=organization_id,
            new_counter=new_counter,
            project=project,
            context=context,
            column_context=column_context,
            color=color,
        )
        self.session.add(temp_interface)
        return temp_interface

    def get_by_id(self, id: str) -> Optional[TempInterface]:
        """
        Get temp interface by ID.

        :param id: Temp interface ID
        :return: TempInterface if found, None otherwise
        """
        query = select(TempInterface).where(TempInterface.id == id)
        return self.session.execute(query).scalar_one_or_none()

    def get_by_user(self, user_id: str) -> List[TempInterface]:
        """
        Get all temp interfaces for a user.

        :param user_id: User ID
        :return: List of TempInterface instances
        """
        query = select(TempInterface).where(TempInterface.user_id == user_id)
        return list(self.session.execute(query).scalars().fetchall())

    def get_by_organization(self, organization_id: int) -> List[TempInterface]:
        """
        Get all temp interfaces for an organization.

        :param organization_id: Organization ID
        :return: List of TempInterface instances
        """
        query = select(TempInterface).where(
            TempInterface.organization_id == organization_id,
        )
        return list(self.session.execute(query).scalars().fetchall())

    def delete(self, id: str) -> bool:
        """
        Delete temp interface by ID.

        :param id: Temp interface ID
        :return: True if deleted, False if not found
        """
        temp_interface = self.get_by_id(id)
        if temp_interface:
            self.session.delete(temp_interface)
            return True
        return False

    def update(
        self,
        id: str,
        items: Optional[str] = None,
        new_counter: Optional[int] = None,
        project: Optional[str] = None,
        context: Optional[str] = None,
        column_context: Optional[str] = None,
        color: Optional[str] = None,
    ) -> Optional[TempInterface]:
        """
        Update temp interface.

        :param id: Temp interface ID
        :param items: Updated items JSON
        :param new_counter: Updated counter
        :param project: Updated project
        :param context: Updated context
        :param column_context: Updated column context
        :param color: Updated color
        :return: Updated TempInterface if found, None otherwise
        """
        temp_interface = self.get_by_id(id)
        if not temp_interface:
            return None

        if items is not None:
            temp_interface.items = items
        if new_counter is not None:
            temp_interface.new_counter = new_counter
        if project is not None:
            temp_interface.project = project
        if context is not None:
            temp_interface.context = context
        if column_context is not None:
            temp_interface.column_context = column_context
        if color is not None:
            temp_interface.color = color

        return temp_interface
